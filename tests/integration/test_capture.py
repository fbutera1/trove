"""Integration tests for the capture hook.

Each test drives the real `trove_capture` against a real temp `trove.db`.
The `capture_db` fixture (conftest.py) handles DB path injection and schema init.
"""

import logging
import sqlite3
import time

import pytest

from trove.capture import trove_capture
from tests.fixtures import (
    FakeMessageEvent,
    FakeSessionSource,
    OPERATOR_ID,
    OPERATOR_UUID,
    _get_nuggets,
    _count_nuggets,
)


# ── T1: Insert Nugget with status='captured' and exact raw_content ──


def test_T1_inserts_captured_nugget_with_exact_raw_content(capture_db):
    """T1: A pre_gateway_dispatch call inserts a Nugget row with
    status='captured' and raw_content equal to the exact inbound event.text.
    """
    text = "need to reorder lobster clasps"
    event = FakeMessageEvent(text=text)

    result = trove_capture(event, gateway=None, session_store=None)

    assert result is None
    rows = _get_nuggets(capture_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "captured"
    assert row["raw_content"] == text
    assert row["source"] == "signal"


# ── T2: Row exists before any LLM/enrich step ───────────────────────


def test_T2_row_exists_before_enrich(capture_db):
    """T2: The nuggets row exists immediately after trove_capture,
    with no enrich call. This proves the ordering invariant:
    the row is safe before any LLM step runs.
    """
    text = "buy milk before noon"
    event = FakeMessageEvent(text=text)

    result = trove_capture(event, gateway=None, session_store=None)

    # Assert immediately — no enrich step
    assert result is None
    rows = _get_nuggets(capture_db)
    assert len(rows) == 1
    assert rows[0]["raw_content"] == text
    assert rows[0]["status"] == "captured"
    # AI fields are null (not yet enriched)
    assert rows[0]["classification"] is None
    assert rows[0]["summary"] is None
    assert rows[0]["entities"] is None


# ── T3: Returns None on DB write failure ────────────────────────────


def test_T3_returns_none_on_write_failure(capture_db, monkeypatch):
    """T3: Capture returns None (allow) even on its own exception —
    dispatch always proceeds.
    """
    import trove.db

    def fake_write_transaction(conn, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(trove.db, "write_transaction", fake_write_transaction)

    event = FakeMessageEvent(text="test failure handling")

    # Must not raise — must return None
    result = trove_capture(event, gateway=None, session_store=None)
    assert result is None

    # No row should have been inserted (transaction rolled back)
    count = _count_nuggets(capture_db)
    assert count == 0


# ── T4: Internal events are skipped ─────────────────────────────────


def test_T4_internal_events_skipped(capture_db):
    """T4: Internal/system events produce no nuggets row and return None."""
    event = FakeMessageEvent(text="system ping", internal=True)

    result = trove_capture(event, gateway=None, session_store=None)

    assert result is None
    count = _count_nuggets(capture_db)
    assert count == 0


# ── T5: Unique Trove-generated message_id ───────────────────────────


def test_T5_unique_message_ids(capture_db):
    """T5: Each capture gets a unique, Trove-generated message_id.
    Two captures yield two distinct ids.
    """
    event1 = FakeMessageEvent(text="first thought")
    event2 = FakeMessageEvent(text="second thought")

    trove_capture(event1, gateway=None, session_store=None)
    trove_capture(event2, gateway=None, session_store=None)

    rows = _get_nuggets(capture_db)
    assert len(rows) == 2

    ids = {row["message_id"] for row in rows}
    assert len(ids) == 2  # Two distinct IDs

    # Both IDs are non-empty strings (Trove-generated, not None)
    for row in rows:
        assert isinstance(row["message_id"], str)
        assert len(row["message_id"]) > 0


# ── T9: created_at from timestamp_ms and time.time() fallback ───────


def test_T9_created_at_from_envelope_timestamp(capture_db):
    """T9a: created_at equals the Signal envelope timestamp_ms (ms→s)
    when present.
    """
    timestamp_ms = 1700000000000
    event = FakeMessageEvent(
        text="timestamped message",
        raw_message={"timestamp_ms": timestamp_ms},
    )

    trove_capture(event, gateway=None, session_store=None)

    rows = _get_nuggets(capture_db)
    assert len(rows) == 1
    assert rows[0]["created_at"] == 1700000000.0  # ms → s


def test_T9_created_at_fallback_to_time_time(capture_db):
    """T9b: created_at falls back to time.time() when timestamp_ms is absent."""
    event = FakeMessageEvent(
        text="no timestamp",
        raw_message={},  # No timestamp_ms
    )

    before = time.time()
    trove_capture(event, gateway=None, session_store=None)
    after = time.time()

    rows = _get_nuggets(capture_db)
    assert len(rows) == 1
    created_at = rows[0]["created_at"]
    assert before <= created_at <= after  # Within the call window


# ── T29: Non-allowlisted sender → no row ────────────────────────────


def test_T29_non_allowlisted_sender_no_nugget(capture_db, monkeypatch):
    """T29: A third-party sender (user_id not in allowlist) produces no
    Nugget row and trove_capture returns None (dispatch proceeds).
    The skip logs at DEBUG, not WARNING.
    """
    # Allowlist contains only the operator; Phyllis is not in it.
    # TROVE_CAPTURE_SENDERS is the explicit override (strict — it is the
    # only allowlist when set), so the fixture's derived TROVE_PEOPLE
    # list cannot widen it.
    monkeypatch.setenv("TROVE_CAPTURE_SENDERS", OPERATOR_ID)
    event = FakeMessageEvent(
        text="hi from Phyllis",
        source=FakeSessionSource(
            user_id="+13125550102",
            user_name="Phyllis",
        ),
    )

    result = trove_capture(event, gateway=None, session_store=None)

    assert result is None
    assert _count_nuggets(capture_db) == 0


# ── T30: Both env vars unset → deny + WARNING + throttle ────────────


def test_T30_both_unset_deny_with_warning_and_throttle(capture_db, monkeypatch, caplog):
    """T30: When all three capture-allowlist sources (TROVE_PEOPLE,
    TROVE_CAPTURE_SENDERS, SIGNAL_ALLOWED_USERS) are unset, capture is
    denied (fail-safe) and a DISABLED WARNING is emitted, throttled so a
    second call within ~60s does NOT log again.
    """
    import trove.capture

    monkeypatch.delenv("TROVE_PEOPLE", raising=False)
    monkeypatch.delenv("TROVE_CAPTURE_SENDERS", raising=False)
    monkeypatch.delenv("SIGNAL_ALLOWED_USERS", raising=False)

    # Reset the module-level throttle so the test is deterministic.
    trove.capture._disabled_warn_ts = 0.0

    caplog.set_level(logging.WARNING)

    event = FakeMessageEvent(text="hi")
    result = trove_capture(event, gateway=None, session_store=None)

    assert result is None
    assert _count_nuggets(capture_db) == 0

    # First call: WARNING emitted
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "DISABLED" in r.message
    ]
    assert len(warnings) == 1

    # Second call within throttle window: no new WARNING
    caplog.clear()
    result2 = trove_capture(event, gateway=None, session_store=None)
    assert result2 is None
    warnings2 = [
        r for r in caplog.records if r.levelno == logging.WARNING and "DISABLED" in r.message
    ]
    assert len(warnings2) == 0


# ── T31: Allowlist match via user_id or user_id_alt ─────────────────


def test_T31_allowlist_match_via_user_id(capture_db):
    """T31a: An allowlisted sender matches via source.user_id → row created."""
    event = FakeMessageEvent(
        text="operator thought",
        source=FakeSessionSource(user_id=OPERATOR_ID, user_name="Alex"),
    )

    result = trove_capture(event, gateway=None, session_store=None)

    assert result is None
    rows = _get_nuggets(capture_db)
    assert len(rows) == 1
    assert rows[0]["raw_content"] == "operator thought"


def test_T31_allowlist_match_via_user_id_alt(capture_db, monkeypatch):
    """T31b: An allowlisted sender matches via source.user_id_alt (UUID)
    → row created. The fixture's derived TROVE_PEOPLE list (operator
    phone + UUID) is the allowlist — no explicit override set."""
    event = FakeMessageEvent(
        text="operator thought via UUID",
        source=FakeSessionSource(user_id=None, user_id_alt=OPERATOR_UUID),
    )

    result = trove_capture(event, gateway=None, session_store=None)

    assert result is None
    rows = _get_nuggets(capture_db)
    assert len(rows) == 1
    assert rows[0]["raw_content"] == "operator thought via UUID"


# ── T32: SIGNAL_ALLOWED_USERS fallback + * deny ─────────────────────


def test_T32_fallback_signal_allowed_users(capture_db, monkeypatch):
    """T32a: TROVE_CAPTURE_SENDERS and TROVE_PEOPLE unset but
    SIGNAL_ALLOWED_USERS set → capture proceeds for that sender (the
    Hermes legacy fallback works)."""
    monkeypatch.delenv("TROVE_PEOPLE", raising=False)
    monkeypatch.delenv("TROVE_CAPTURE_SENDERS", raising=False)
    monkeypatch.setenv("SIGNAL_ALLOWED_USERS", OPERATOR_UUID)

    # Reset throttle so no stale WARNING from a prior test.
    import trove.capture

    trove.capture._disabled_warn_ts = 0.0

    event = FakeMessageEvent(text="hi with fallback")
    result = trove_capture(event, gateway=None, session_store=None)

    assert result is None
    rows = _get_nuggets(capture_db)
    assert len(rows) == 1


def test_T32_star_open_mode_deny(capture_db, monkeypatch, caplog):
    """T32b: SIGNAL_ALLOWED_USERS=* (with TROVE_PEOPLE /
    TROVE_CAPTURE_SENDERS unset) → deny (open mode does not re-open
    capture). A DISABLED WARNING is emitted.
    """
    import trove.capture

    monkeypatch.delenv("TROVE_PEOPLE", raising=False)
    monkeypatch.delenv("TROVE_CAPTURE_SENDERS", raising=False)
    monkeypatch.setenv("SIGNAL_ALLOWED_USERS", "*")

    trove.capture._disabled_warn_ts = 0.0

    caplog.set_level(logging.WARNING)

    event = FakeMessageEvent(text="hi from everyone")
    result = trove_capture(event, gateway=None, session_store=None)

    assert result is None
    assert _count_nuggets(capture_db) == 0

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "DISABLED" in r.message
    ]
    assert len(warnings) == 1


# ── T43: UUID capture → raw author → named task (e2e regression) ────


def test_T43_uuid_capture_resolves_named_task(capture_db, monkeypatch):
    """T43: End-to-end regression — a partner-world capture (UUID
    user_id + user_name) stores the RAW UUID in the author column (never
    a "Name (uuid)" composite), and nugget_tasks resolves the display
    name via a UUID key in TROVE_PEOPLE.

    This is the exact production bug class the config consolidation
    fixes: pre-v4 capture stored "Frank (uuid)" while TROVE_PEOPLE was
    keyed by ID, so the lookup never matched. The fixtures model the
    partner world (UUID sender IDs), so a regression that re-introduces
    composite authors or phone-only keys fails here.
    """
    from trove.tools import nugget_tasks

    # T43 captures on the derived path (the fixture's TROVE_PEOPLE list,
    # re-set below with the same dual-format shape) — it is about name
    # resolution, not the allowlist (T46–T48 cover the allowlist chain).
    # Partner-world TROVE_PEOPLE: the same person under both ID shapes
    # (phone for note-to-self, UUID for DMs).
    monkeypatch.setenv(
        "TROVE_PEOPLE", f"{OPERATOR_ID}:Alex,{OPERATOR_UUID}:Alex"
    )

    # Capture an inbound partner DM (UUID user_id, user_name present).
    event = FakeMessageEvent(
        text="order lobster clasps for the weekend",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )
    trove_capture(event, gateway=None, session_store=None)

    rows = _get_nuggets(capture_db)
    assert len(rows) == 1
    # The author column stores the raw sender ID — NOT a composite.
    assert rows[0]["author"] == OPERATOR_UUID

    # Enrich into an open task so nugget_tasks enumerates it.
    from trove.tools import nugget_enrich

    mid = rows[0]["message_id"]
    nugget_enrich(
        mid,
        classification="task",
        summary="Order lobster clasps",
        confidence=0.9,
        status="enriched",
        db_path=capture_db,
    )

    results = nugget_tasks(db_path=capture_db)
    assert len(results) == 1
    task = results[0]
    assert task["author"] == OPERATOR_UUID
    # Name resolved from the UUID key — this was None in production pre-fix.
    assert task["author_label"] == "Alex"
    # Self-assigned → assignee_display falls back to the author's name.
    assert task["assignee_display"] == "Alex"


# ── T46: Derived capture allowlist from TROVE_PEOPLE keys ───────────


def test_T46_derived_allowlist_uuid_and_phone(capture_db, monkeypatch):
    """T46: With TROVE_CAPTURE_SENDERS unset, the capture allowlist is
    derived from TROVE_PEOPLE keys. Both ID shapes of the mapped person
    are captured (UUID for partner DMs, phone for note-to-self); a
    third-party sender is denied.

    This is the preferred deploy path after Phase 2: TROVE_PEOPLE is
    the single list to maintain.
    """
    monkeypatch.delenv("TROVE_CAPTURE_SENDERS", raising=False)
    # The fixture already sets TROVE_PEOPLE to the operator's phone + UUID.

    # Partner-world capture (UUID user_id) → row created.
    event_uuid = FakeMessageEvent(
        text="uuid shape thought",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )
    assert trove_capture(event_uuid, gateway=None, session_store=None) is None
    assert _count_nuggets(capture_db) == 1

    # Note-to-self capture (phone user_id) → row created.
    event_phone = FakeMessageEvent(
        text="phone shape thought",
        source=FakeSessionSource(user_id=OPERATOR_ID, user_name="Alex"),
    )
    assert trove_capture(event_phone, gateway=None, session_store=None) is None
    assert _count_nuggets(capture_db) == 2

    # A third party is not in TROVE_PEOPLE → denied.
    event_stranger = FakeMessageEvent(
        text="stranger thought",
        source=FakeSessionSource(
            user_id="00000000-0000-4000-8000-000000000099", user_name="Phyllis"
        ),
    )
    assert trove_capture(event_stranger, gateway=None, session_store=None) is None
    assert _count_nuggets(capture_db) == 2


# ── T47: Explicit TROVE_CAPTURE_SENDERS override is strict ──────────


def test_T47_explicit_override_is_strict(capture_db, monkeypatch):
    """T47: When TROVE_CAPTURE_SENDERS IS set, it is the ONLY capture
    allowlist — TROVE_PEOPLE keys do NOT widen it. A person in
    TROVE_PEOPLE but absent from the explicit list is not captured.

    Use case: capturing from fewer people than TROVE_PEOPLE defines.
    """
    monkeypatch.setenv("TROVE_CAPTURE_SENDERS", OPERATOR_UUID)
    # Fixture TROVE_PEOPLE includes the operator's phone + UUID.
    # The explicit list contains only the UUID.

    # UUID sender → in the explicit list → captured.
    event_uuid = FakeMessageEvent(
        text="explicit list capture",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )
    assert trove_capture(event_uuid, gateway=None, session_store=None) is None
    assert _count_nuggets(capture_db) == 1

    # Phone sender → in TROVE_PEOPLE but NOT in the explicit list →
    # denied (the override is strict, not additive).
    event_phone = FakeMessageEvent(
        text="phone not in explicit list",
        source=FakeSessionSource(user_id=OPERATOR_ID, user_name="Alex"),
    )
    assert trove_capture(event_phone, gateway=None, session_store=None) is None
    assert _count_nuggets(capture_db) == 1


# ── T48: Echo-capture guard on the derived path ─────────────────────


def test_T48_bot_number_in_allowed_users_not_captured(capture_db, monkeypatch):
    """T48: Echo-capture guard — the bot's own number is in
    SIGNAL_ALLOWED_USERS (required for self-chat auth) but is never in
    TROVE_PEOPLE. With TROVE_CAPTURE_SENDERS unset, the derived
    TROVE_PEOPLE allowlist does NOT fall through to SIGNAL_ALLOWED_USERS
    (it is the first resolvable source), so the bot number is denied.

    If the bot number were capture-allowed, the bot would capture its
    own notifications/digests as Nuggets — the load-bearing echo guard.
    """
    # A "bot number" present in SIGNAL_ALLOWED_USERS but absent from
    # TROVE_PEOPLE (which the fixture sets to operator phone + UUID).
    # Obviously-fake placeholder in the test's +131****010N series.
    BOT_NUMBER = "+131****0103"
    monkeypatch.delenv("TROVE_CAPTURE_SENDERS", raising=False)
    monkeypatch.setenv("SIGNAL_ALLOWED_USERS", BOT_NUMBER)

    event_bot = FakeMessageEvent(
        text="bot self-echo",
        source=FakeSessionSource(user_id=BOT_NUMBER, user_name="TroveBot"),
    )
    assert trove_capture(event_bot, gateway=None, session_store=None) is None
    assert _count_nuggets(capture_db) == 0
