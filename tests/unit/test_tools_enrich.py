"""Unit tests for the enrichment tool.

Each test uses the `enrich_db` fixture (conftest.py) for a temp DB and
`insert_captured_nugget` (fixtures.py) for a realistic captured row.
"""

import json

import pytest

from trove.tools import nugget_enrich
from tests.fixtures import _get_nuggets, insert_captured_nugget


# ── T10: Validation of classification, status, confidence ────────────


def test_T10_validation(enrich_db):
    """T10: nugget_enrich validates classification against the fixed taxonomy
    and rejects invalid values. Also validates status and confidence.
    """
    mid = insert_captured_nugget(enrich_db, text="test validation")

    # Invalid classification raises ValueError
    with pytest.raises(ValueError, match="Invalid classification"):
        nugget_enrich(mid, classification="appointment")

    # Invalid status raises ValueError
    with pytest.raises(ValueError, match="Invalid status"):
        nugget_enrich(mid, status="captured")

    with pytest.raises(ValueError, match="Invalid status"):
        nugget_enrich(mid, status="bogus")

    # Out-of-range confidence raises ValueError
    with pytest.raises(ValueError, match="Invalid confidence"):
        nugget_enrich(mid, confidence=1.5)

    with pytest.raises(ValueError, match="Invalid confidence"):
        nugget_enrich(mid, confidence=-0.1)

    # Valid values pass and the row updates
    result = nugget_enrich(
        mid,
        classification="task",
        entities=["dentist"],
        summary="Need to call dentist",
        status="enriched",
        confidence=0.9,
    )
    assert result["classification"] == "task"
    assert result["status"] == "enriched"
    assert result["confidence"] == 0.9


# ── T11: Idempotent re-enrich ────────────────────────────────────────


def test_T11_idempotent_reenrich(enrich_db):
    """T11: Re-enrich with different AI fields overwrites AI fields,
    but leaves raw_content, message_id, created_at, author, source unchanged.
    """
    original_text = "need to reorder lobster clasps by Friday"
    mid = insert_captured_nugget(enrich_db, text=original_text)

    # First enrich
    result1 = nugget_enrich(
        mid,
        classification="task",
        entities=["lobster clasps"],
        summary="Reorder lobster clasps",
        confidence=0.85,
        model="model-a",
    )

    # Second enrich with different fields
    result2 = nugget_enrich(
        mid,
        classification="note",
        entities=["lobster clasps", "Friday"],
        summary="Reorder lobster clasps by Friday deadline",
        confidence=0.95,
        model="model-b",
    )

    # AI fields overwritten
    assert result2["classification"] == "note"
    assert json.loads(result2["entities"]) == ["lobster clasps", "Friday"]
    assert result2["summary"] == "Reorder lobster clasps by Friday deadline"
    assert result2["confidence"] == 0.95

    # Immutable fields unchanged
    assert result2["raw_content"] == original_text
    assert result2["message_id"] == mid
    assert result2["created_at"] == result1["created_at"]
    assert result2["author"] == result1["author"]
    assert result2["source"] == result1["source"]


# ── T6: raw_content write-once at enrich layer ───────────────────────


def test_T6_raw_content_unchanged_by_enrich(enrich_db):
    """T6: After a successful enrich, raw_content still equals the original
    capture text. The tool never writes that column.
    """
    original_text = "buy milk before noon at the corner store"
    mid = insert_captured_nugget(enrich_db, text=original_text)

    result = nugget_enrich(
        mid,
        classification="task",
        entities=["milk", "corner store"],
        summary="Buy milk before noon",
        confidence=0.9,
    )

    # raw_content is unchanged
    assert result["raw_content"] == original_text

    # Verify directly from DB
    rows = _get_nuggets(enrich_db)
    assert len(rows) == 1
    assert rows[0]["raw_content"] == original_text


# ── T12: Metadata provenance tracking ────────────────────────────────


def test_T12_metadata_provenance(enrich_db):
    """T12: After enrich, metadata JSON contains model, attempts==1,
    last_enriched_at timestamp, and updated_at > original.
    After second enrich, attempts==2 and model is the new value.
    """
    mid = insert_captured_nugget(enrich_db, text="test provenance")

    original_rows = _get_nuggets(enrich_db)
    original_updated_at = original_rows[0]["updated_at"]

    # First enrich
    result1 = nugget_enrich(
        mid,
        classification="fact",
        summary="A test nugget",
        confidence=0.7,
        model="test-model-v1",
    )

    metadata1 = json.loads(result1["metadata"])
    assert metadata1["model"] == "test-model-v1"
    assert metadata1["attempts"] == 1
    assert "last_enriched_at" in metadata1
    assert result1["updated_at"] > original_updated_at

    # Second enrich with different model
    result2 = nugget_enrich(
        mid,
        classification="note",
        summary="Updated test nugget",
        confidence=0.8,
        model="test-model-v2",
    )

    metadata2 = json.loads(result2["metadata"])
    assert metadata2["model"] == "test-model-v2"
    assert metadata2["attempts"] == 2
    assert result2["updated_at"] > result1["updated_at"]


# ── T38: due_at + assignee stamping ──────────────────────────────────


def test_T38_due_assignee_stamping(enrich_db):
    """T38: nugget_enrich stamps due_at + assignee; idempotent re-enrich
    overwrites both; never touches raw_content; null assignee allowed (self)."""
    original_text = "need to reorder lobster clasps by Friday"
    mid = insert_captured_nugget(enrich_db, text=original_text)

    # First enrich with due_at and assignee
    result1 = nugget_enrich(
        mid,
        classification="task",
        entities=["lobster clasps"],
        summary="Reorder lobster clasps by Friday",
        confidence=0.9,
        due_at="2026-08-15",
        assignee="Alex",
    )

    # Verify due_at is normalized to epoch (float)
    assert isinstance(result1["due_at"], float)
    # 2026-08-15 midnight local should be ~1786848000 (varies by TZ)
    assert result1["due_at"] > 1_700_000_000  # sanity: after 2023
    assert result1["assignee"] == "Alex"
    # raw_content unchanged
    assert result1["raw_content"] == original_text

    # Re-enrich with None values — should overwrite to NULL
    result2 = nugget_enrich(
        mid,
        classification="task",
        due_at=None,
        assignee=None,
    )
    assert result2["due_at"] is None
    assert result2["assignee"] is None
    # raw_content still unchanged
    assert result2["raw_content"] == original_text

    # Null assignee (self) should succeed
    result3 = nugget_enrich(
        mid,
        classification="task",
        due_at="2026-09-01",
        assignee=None,  # self
    )
    assert result3["assignee"] is None
    assert isinstance(result3["due_at"], float)


# ── T39: due_at + assignee validation ────────────────────────────────


def test_T39_due_at_validation(enrich_db):
    """T39: nugget_enrich validates due_at (ISO date/datetime/epoch;
    unparseable -> ValueError) and assignee (non-empty or None; empty -> ValueError)."""
    mid = insert_captured_nugget(enrich_db, text="test validation")

    # ISO date string -> success
    result = nugget_enrich(mid, classification="task", due_at="2026-08-15")
    assert isinstance(result["due_at"], float)

    # ISO datetime string -> success
    result = nugget_enrich(mid, classification="task", due_at="2026-08-15T09:00:00")
    assert isinstance(result["due_at"], float)

    # Numeric epoch (int) -> success
    result = nugget_enrich(mid, classification="task", due_at=1723737600)
    assert result["due_at"] == 1723737600.0

    # Numeric epoch (float) -> success
    result = nugget_enrich(mid, classification="task", due_at=1723737600.5)
    assert result["due_at"] == 1723737600.5

    # None -> passes through as NULL
    result = nugget_enrich(mid, classification="task", due_at=None)
    assert result["due_at"] is None

    # Unparseable string -> ValueError
    with pytest.raises(ValueError, match="due_at"):
        nugget_enrich(mid, classification="task", due_at="not-a-date")

    # ── Assignee validation ──

    # Valid assignee -> success
    result = nugget_enrich(mid, classification="task", assignee="Alex")
    assert result["assignee"] == "Alex"

    # None (self) -> success
    result = nugget_enrich(mid, classification="task", assignee=None)
    assert result["assignee"] is None

    # Empty string -> ValueError
    with pytest.raises(ValueError, match="assignee"):
        nugget_enrich(mid, classification="task", assignee="")

    # Whitespace-only -> ValueError
    with pytest.raises(ValueError, match="assignee"):
        nugget_enrich(mid, classification="task", assignee="   ")
