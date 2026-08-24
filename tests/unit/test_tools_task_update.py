"""Unit tests for the nugget_task_update tool — the atomic task-update fix.

Covers the regression scenarios from
deploy/shared/skills/trove-task-association/references/completion-association-incident.md:

  - resolving an existing task updates the ORIGINAL task, not the follow-up
  - a scheduling follow-up stamps due_at onto the original task
  - a completion message is linked bidirectionally but NOT reclassified
  - non-task rows are rejected (the core confusion the bug exploited)
  - sentinel semantics: omitted fields are left unchanged; explicit None clears
  - raw_content is never modified
"""

import json

import pytest

from trove.tools import nugget_task_update, _handle_nugget_task_update, _UNSET
from tests.fixtures import insert_captured_nugget, _get_nuggets


# ── Helpers ───────────────────────────────────────────────────────────


def _enrich_task(db_path, mid, *, summary="task", due_at=None, assignee=None,
                 status="enriched"):
    """Enrich a captured row into a task via nugget_enrich (real path)."""
    from trove.tools import nugget_enrich

    kwargs = {
        "classification": "task",
        "summary": summary,
        "status": status,
        "confidence": 0.9,
    }
    if due_at is not None:
        kwargs["due_at"] = due_at
    if assignee is not None:
        kwargs["assignee"] = assignee
    nugget_enrich(mid, **kwargs, db_path=db_path)


def _get_row(db_path, mid):
    """Return one nugget row as a dict."""
    rows = {r["message_id"]: r for r in _get_nuggets(db_path)}
    return rows[mid]


# ── Happy path: resolve an existing task from a completion message ──


def test_resolve_original_task_not_followup(enrich_db):
    """The car-service completion incident: a completion message must resolve
    the ORIGINAL task's id, and the follow-up must remain its own immutable
    record.
    """
    db = enrich_db
    original = insert_captured_nugget(db, text="take the car in for an oil change")
    _enrich_task(db, original)

    completion = insert_captured_nugget(db, text="oil change done, mark it resolved")
    _enrich_task(db, completion)  # agent mistakenly enriched the follow-up too

    result = nugget_task_update(
        task_id=original,
        status="resolved",
        completion_message_id=completion,
        db_path=db,
    )

    # Original task is resolved
    assert result["status"] == "resolved"
    assert result["classification"] == "task"  # unchanged
    assert result["raw_content"] == "take the car in for an oil change"  # immutable

    # The completion message is linked, but NOT reclassified by this tool
    comp = _get_row(db, completion)
    assert comp["classification"] == "task"  # untouched by task_update
    assert comp["raw_content"] == "oil change done, mark it resolved"
    assert json.loads(comp["links"]) == [original]

    # Bidirectional link on the task
    assert json.loads(result["links"]) == [completion]
    assert result["completion_message_linked"] is True

    # The follow-up is NOT marked resolved by this tool
    assert comp["status"] != "resolved" or comp["status"] == "enriched"


def test_resolved_task_drops_from_open_tasks(enrich_db):
    """After resolving, nugget_tasks no longer returns the original task."""
    from trove.tools import nugget_tasks

    db = enrich_db
    original = insert_captured_nugget(db, text="cancel the streaming subscription")
    _enrich_task(db, original)
    completion = insert_captured_nugget(db, text="I checked, subscription cancels next month, done")
    _enrich_task(db, completion)

    before = nugget_tasks(horizon="all", db_path=db)
    assert any(t["message_id"] == original for t in before)

    nugget_task_update(original, status="resolved",
                       completion_message_id=completion, db_path=db)

    after = nugget_tasks(horizon="all", db_path=db)
    assert not any(t["message_id"] == original for t in after), \
        "resolved task must not appear in open tasks"


# ── Scheduling follow-up: stamp due_at onto the original ────────────


def test_schedule_stamps_due_at_onto_original(enrich_db):
    """The grooming/gutters class: a scheduling follow-up updates the ORIGINAL
    task's due_at instead of creating a second open task.
    """
    db = enrich_db
    original = insert_captured_nugget(db, text="book the dog's grooming")
    _enrich_task(db, original)
    assert _get_row(db, original)["due_at"] is None

    followup = insert_captured_nugget(db, text="grooming appointment next week")

    result = nugget_task_update(
        task_id=original,
        due_at="2026-08-02",
        completion_message_id=followup,
        db_path=db,
    )

    # Original got the due date; the follow-up did not become a task record
    assert result["due_at"] is not None
    assert result["status"] == "enriched"  # still open, just scheduled
    comp = _get_row(db, followup)
    assert comp["due_at"] is None  # follow-up's metadata untouched


def test_assignee_update(enrich_db):
    """An assignment follow-up updates the original task's assignee."""
    db = enrich_db
    original = insert_captured_nugget(db, text="clean the gutters on Thursday")
    _enrich_task(db, original)
    assert _get_row(db, original)["assignee"] is None

    followup = insert_captured_nugget(db, text="Alex will do the gutters")

    result = nugget_task_update(
        task_id=original,
        assignee="Alex",
        completion_message_id=followup,
        db_path=db,
    )

    assert result["assignee"] == "Alex"
    assert json.loads(result["links"]) == [followup]


# ── Sentinel semantics: omit vs explicit None ───────────────────────


def test_omitted_fields_left_unchanged(enrich_db):
    """Passing only status leaves due_at/assignee as they were."""
    db = enrich_db
    original = insert_captured_nugget(db, text="order clasps")
    _enrich_task(db, original, due_at="2026-08-15", assignee="Alex")

    result = nugget_task_update(task_id=original, status="resolved", db_path=db)

    assert result["status"] == "resolved"
    assert result["due_at"] == _get_row(db, original)["due_at"]  # unchanged
    assert result["assignee"] == "Alex"  # unchanged


def test_explicit_none_clears_fields(enrich_db):
    """Explicit None clears due_at/assignee to NULL."""
    db = enrich_db
    original = insert_captured_nugget(db, text="order clasps")
    _enrich_task(db, original, due_at="2026-08-15", assignee="Alex")

    result = nugget_task_update(
        task_id=original, due_at=None, assignee=None, db_path=db
    )

    assert result["due_at"] is None
    assert result["assignee"] is None
    assert result["status"] == "enriched"  # unchanged


def test_clear_due_date_drops_from_today(enrich_db):
    """Clearing due_at moves a scheduled task to 'unscheduled'."""
    from trove.tools import nugget_tasks

    db = enrich_db
    original = insert_captured_nugget(db, text="order clasps")
    _enrich_task(db, original, due_at="2026-08-15")

    nugget_task_update(task_id=original, due_at=None, db_path=db)

    unscheduled = nugget_tasks(horizon="unscheduled", db_path=db)
    assert any(t["message_id"] == original for t in unscheduled)


# ── Validation / rejection ──────────────────────────────────────────


def test_reject_non_task_row(enrich_db):
    """The core guard: task_id must be classification='task'. A follow-up
    mistakenly classified as a note must not be silently 'updated' as a task.
    """
    from trove.tools import nugget_enrich

    db = enrich_db
    note = insert_captured_nugget(db, text="a fact, not a task")
    nugget_enrich(note, classification="note", summary="x",
                  status="enriched", confidence=0.9, db_path=db)

    with pytest.raises(TypeError, match="not 'task'"):
        nugget_task_update(task_id=note, status="resolved", db_path=db)


def test_reject_missing_task_id(enrich_db):
    db = enrich_db
    with pytest.raises(KeyError, match="No Nugget found"):
        nugget_task_update(task_id="does-not-exist", status="resolved",
                           db_path=db)


def test_reject_invalid_status(enrich_db):
    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original)

    with pytest.raises(ValueError, match="Invalid status"):
        nugget_task_update(task_id=original, status="captured", db_path=db)

    with pytest.raises(ValueError, match="Invalid status"):
        nugget_task_update(task_id=original, status="failed", db_path=db)

    with pytest.raises(ValueError, match="Invalid status"):
        nugget_task_update(task_id=original, status="bogus", db_path=db)


def test_reject_missing_completion_message_id(enrich_db):
    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original)

    with pytest.raises(KeyError, match="does not exist"):
        nugget_task_update(task_id=original, status="resolved",
                           completion_message_id="nope", db_path=db)


def test_reject_invalid_due_at(enrich_db):
    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original)

    with pytest.raises(ValueError, match="ISO 8601"):
        nugget_task_update(task_id=original, due_at="not-a-date", db_path=db)


# ── Immutability ────────────────────────────────────────────────────


def test_raw_content_never_modified(enrich_db):
    db = enrich_db
    original = insert_captured_nugget(db, text="original task text")
    _enrich_task(db, original)
    before = _get_row(db, original)["raw_content"]

    nugget_task_update(task_id=original, status="resolved",
                       due_at="2026-09-01", assignee="Alex", db_path=db)

    assert _get_row(db, original)["raw_content"] == before


# ── Idempotency / re-update ──────────────────────────────────────────


def test_re_update_is_idempotent(enrich_db):
    """Calling task_update again overwrites metadata and does not duplicate links."""
    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original)
    followup = insert_captured_nugget(db, text="done")

    nugget_task_update(original, status="resolved",
                       completion_message_id=followup, db_path=db)
    result = nugget_task_update(original, status="resolved",
                                 completion_message_id=followup, db_path=db)

    # Link appears once, not twice
    assert json.loads(result["links"]) == [followup]
    comp = _get_row(db, followup)
    assert json.loads(comp["links"]) == [original]


def test_self_link_not_created(enrich_db):
    """If task_id == completion_message_id, do not create a self-referential link."""
    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original)

    result = nugget_task_update(original, status="resolved",
                                completion_message_id=original, db_path=db)
    links = json.loads(result["links"]) if result["links"] else []
    assert original not in links


# ── Provenance ───────────────────────────────────────────────────────


def test_metadata_records_task_update(enrich_db):
    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original)
    followup = insert_captured_nugget(db, text="done")

    result = nugget_task_update(original, status="resolved",
                                completion_message_id=followup,
                                model="test-model", db_path=db)
    md = json.loads(result["metadata"])
    assert md.get("task_updates") == 1
    assert md.get("completion_message_id") == followup
    assert md.get("model") == "test-model"
    assert "last_task_update_at" in md


# ── Handler wrapper ──────────────────────────────────────────────────


def test_handler_returns_json_success(enrich_db):
    import trove.db
    trove.db.get_trove_db_path = lambda: enrich_db  # handler resolves path

    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original)

    out = _handle_nugget_task_update({"task_id": original, "status": "resolved"})
    parsed = json.loads(out)
    assert parsed["status"] == "resolved"
    assert parsed["completion_message_linked"] is False


def test_handler_never_raises_on_error(enrich_db):
    out = _handle_nugget_task_update({"task_id": "nope", "status": "resolved"})
    parsed = json.loads(out)
    assert "error" in parsed
    assert parsed["task_id"] == "nope"


def test_handler_omitted_key_means_unchanged(enrich_db):
    """A key absent from the args dict means 'leave unchanged' (the _UNSET
    path), not 'clear'. This is the JSON-caller contract.
    """
    import trove.db
    trove.db.get_trove_db_path = lambda: enrich_db

    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original, due_at="2026-08-15", assignee="Alex")

    # 'due_at' and 'assignee' absent -> unchanged
    out = _handle_nugget_task_update({"task_id": original, "status": "resolved"})
    parsed = json.loads(out)
    assert parsed["status"] == "resolved"
    assert parsed["due_at"] is not None
    assert parsed["assignee"] == "Alex"


def test_handler_explicit_null_clears(enrich_db):
    import trove.db
    trove.db.get_trove_db_path = lambda: enrich_db

    db = enrich_db
    original = insert_captured_nugget(db, text="task")
    _enrich_task(db, original, due_at="2026-08-15", assignee="Alex")

    out = _handle_nugget_task_update(
        {"task_id": original, "due_at": None, "assignee": None}
    )
    parsed = json.loads(out)
    assert parsed["due_at"] is None
    assert parsed["assignee"] is None
