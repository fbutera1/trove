"""Unit tests for partner notification.

Each test drives the real `_notify_partner` / `trove_capture` against a
temp `trove.db`. The `capture_db` fixture handles DB path injection and
schema init. The fixture models the two-person partner household:
`TROVE_PEOPLE` carries the operator (phone + UUID) and the partner
(UUID), so the derived partner map pairs them — T39 exercises that
derived path end-to-end, and the explicit `TROVE_PARTNERS` override is
exercised deliberately by T34–T37 and T50. T33 switches the fixture to the solo
world (1-person `TROVE_PEOPLE`) to pin the empty-map behavior.

Fixtures model the partner world: senders carry UUID `user_id`s and
`TROVE_PARTNERS` is keyed by UUIDs (Signal delivery requires UUIDs —
see README "Signal UUID requirements").
"""

import asyncio
import logging

import pytest

from trove.capture import trove_capture, _notify_partner, _partner_map, logger
from tests.fixtures import (
    FakeAdapter,
    FakeGateway,
    FakeMessageEvent,
    FakeSessionSource,
    OPERATOR_ID,
    OPERATOR_UUID,
    PARTNER_UUID,
    _count_nuggets,
    _get_nuggets,
)

PARTNER_ID = PARTNER_UUID


# ── T33: Solo (1-person TROVE_PEOPLE, no TROVE_PARTNERS) → no-op ────


def test_T33_solo_no_partner_notification(capture_db, monkeypatch):
    """T33: Solo world — TROVE_PARTNERS unset and TROVE_PEOPLE holds a
    SINGLE person (the note-to-self shape), so the derived partner map
    is empty (Q5: solo derives solo behavior, no solo-specific code).
    _notify_partner is a no-op: no adapter resolution, no send call,
    Nugget row still written, hook returns None.
    """
    # Solo mode: one person, no TROVE_PARTNERS.
    monkeypatch.delenv("TROVE_PARTNERS", raising=False)
    monkeypatch.setenv("TROVE_PEOPLE", f"{OPERATOR_ID}:Alex,{OPERATOR_UUID}:Alex")

    fake_adapter = FakeAdapter()
    fake_gateway = FakeGateway(adapter=fake_adapter)

    event = FakeMessageEvent(
        text="solo thought",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )

    result = trove_capture(event, gateway=fake_gateway, session_store=None)

    # Hook returns None (allow)
    assert result is None

    # No adapter resolution attempted
    assert fake_gateway.resolved == []

    # No send attempted
    assert fake_adapter.sent == []

    # Nugget row still written
    assert _count_nuggets(capture_db) == 1


# ── T34: Mapped sender → one notice scheduled ───────────────────────


async def test_T34_mapped_sender_schedules_notice(capture_db, monkeypatch):
    """T34: A mapped sender (TROVE_PARTNERS=sender:partner) schedules
    exactly one notice on the resolved adapter with text
    '{author} added a household memory:\n{excerpt}', excerpt capped at
    500 chars with '...' suffix.
    """
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_ID}")

    fake_adapter = FakeAdapter()
    fake_gateway = FakeGateway(adapter=fake_adapter)

    event = FakeMessageEvent(
        text="remember to buy lobster clasps",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )

    result = trove_capture(event, gateway=fake_gateway, session_store=None)

    assert result is None

    # Let the scheduled asyncio.ensure_future coroutine run
    await asyncio.sleep(0)

    # Exactly one send call
    assert len(fake_adapter.sent) == 1
    dest, text = fake_adapter.sent[0]
    assert dest == PARTNER_ID
    assert text == "Alex added a household memory:\nremember to buy lobster clasps"

    # Nugget row written
    assert _count_nuggets(capture_db) == 1


async def test_T34_excerpt_capped_at_500_chars(capture_db, monkeypatch):
    """T34 (excerpt cap): raw_content longer than 500 chars produces an
    excerpt of exactly 500 chars (497 + '...')."""
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_ID}")

    fake_adapter = FakeAdapter()
    fake_gateway = FakeGateway(adapter=fake_adapter)

    long_text = "x" * 600  # 600 chars — well over the cap
    event = FakeMessageEvent(
        text=long_text,
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )

    trove_capture(event, gateway=fake_gateway, session_store=None)
    await asyncio.sleep(0)

    assert len(fake_adapter.sent) == 1
    dest, text = fake_adapter.sent[0]
    assert dest == PARTNER_ID
    # Excerpt is first 497 chars + "..." = 500 chars
    expected_excerpt = "x" * 497 + "..."
    assert text == f"Alex added a household memory:\n{expected_excerpt}"
    assert len(expected_excerpt) == 500


# ── T35: adapter.send failure → row intact, no raise ────────────────


async def test_T35_send_raises_row_intact(capture_db, monkeypatch):
    """T35a: adapter.send raises an exception → Nugget row intact,
    hook returns None, nothing raises into dispatch, logged at WARNING."""
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_ID}")

    fake_adapter = FakeAdapter(raise_exc=RuntimeError("network down"))
    fake_gateway = FakeGateway(adapter=fake_adapter)

    event = FakeMessageEvent(
        text="during network failure",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )

    # Use monkeypatch to replace the module-level logger before trove_capture
    # runs — the done callback resolves the module-global `logger` at call
    # time, so the monkeypatched recorder is the one it hits.
    fake_logger = logging.getLogger("trove.capture.fake_test")
    fake_logger.handlers = []  # no handlers
    calls = []

    def capture_warning(msg, *args, **kwargs):
        calls.append((msg, args, kwargs))

    fake_logger.warning = capture_warning

    import trove.capture as capture_module
    monkeypatch.setattr(capture_module, "logger", fake_logger)

    # Must not raise
    result = trove_capture(event, gateway=fake_gateway, session_store=None)
    assert result is None

    # Row still written
    assert _count_nuggets(capture_db) == 1

    # Let the scheduled coroutine run so the done callback fires.
    # Two yields are needed: first lets send() start, second lets the
    # done callback fire after send() completes.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The done callback logged a warning
    assert len(calls) >= 1, f"Expected warning call, got {calls}"
    assert "trove notification failed" in calls[0][0]


async def test_T35_send_success_false_row_intact(capture_db, monkeypatch):
    """T35b: adapter.send returns success=False → Nugget row intact,
    hook returns None, nothing raises into dispatch, logged at WARNING."""
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_ID}")

    fake_adapter = FakeAdapter(success=False)
    fake_gateway = FakeGateway(adapter=fake_adapter)

    event = FakeMessageEvent(
        text="partner delivery failed",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )

    # Use monkeypatch to replace the module-level logger before trove_capture
    # runs — the done callback resolves the module-global `logger` at call
    # time, so the monkeypatched recorder is the one it hits.
    fake_logger = logging.getLogger("trove.capture.fake_test2")
    fake_logger.handlers = []  # no handlers
    calls = []

    def capture_warning(msg, *args, **kwargs):
        calls.append((msg, args, kwargs))

    fake_logger.warning = capture_warning

    import trove.capture as capture_module
    monkeypatch.setattr(capture_module, "logger", fake_logger)

    result = trove_capture(event, gateway=fake_gateway, session_store=None)
    assert result is None

    # Row still written
    assert _count_nuggets(capture_db) == 1

    # Let the scheduled coroutine run so the done callback fires.
    # Two yields are needed: first lets send() start, second lets the
    # done callback fire after send() completes.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The done callback logged a warning for success=False
    assert len(calls) >= 1, f"Expected warning call, got {calls}"
    assert "trove notification failed" in calls[0][0]


# ── T36: gateway=None or no running loop → skip quietly ─────────────


def test_T36_gateway_none_skip(capture_db, monkeypatch):
    """T36a: trove_capture with gateway=None and TROVE_PARTNERS set →
    returns None, row written, no raise."""
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_ID}")

    event = FakeMessageEvent(
        text="no gateway available",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )

    result = trove_capture(event, gateway=None, session_store=None)
    assert result is None

    # Row still written
    assert _count_nuggets(capture_db) == 1


def test_T36_no_running_loop_skip(capture_db, monkeypatch, caplog):
    """T36b: _notify_partner called outside a running event loop (or with
    a resolver raising RuntimeError) → returns None quietly, debug log,
    no raise."""
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_ID}")

    caplog.set_level(logging.DEBUG)

    # Build a gateway whose _adapter_for_source raises RuntimeError
    # (simulates no running loop scenario)
    class RuntimeErrorGateway:
        def _adapter_for_source(self, source):
            raise RuntimeError("no running event loop")

    event = FakeMessageEvent(
        text="no loop",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )

    # Call _notify_partner directly — no running loop
    result = _notify_partner(event, RuntimeErrorGateway(), "some text", "mid-123")
    assert result is None

    # Debug log emitted, no exception
    debugs = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "no running event loop" in r.message
    ]
    assert len(debugs) >= 1


# ── T37: Bidirectional partner map ──────────────────────────────────


async def test_T37_bidirectional_partner_map(capture_db, monkeypatch):
    """T37: TROVE_PARTNERS=A:B produces both {A:B} and {B:A} so
    notifications work regardless of which spouse sends."""
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_ID}")

    fake_adapter = FakeAdapter()
    fake_gateway = FakeGateway(adapter=fake_adapter)

    # Partner sends — should notify the operator (reverse direction)
    event = FakeMessageEvent(
        text="partner sent this",
        source=FakeSessionSource(user_id=PARTNER_ID, user_name="Sam"),
    )

    # Allow the partner through the capture allowlist. TROVE_PARTNERS
    # is explicit here (the point of T37); the fixture's TROVE_PEOPLE
    # already includes the partner, so the derived allowlist covers it —
    # the explicit TROVE_CAPTURE_SENDERS override is set to pin the
    # strict-override path (it does not widen TROVE_PEOPLE).
    monkeypatch.setenv("TROVE_CAPTURE_SENDERS", f"{OPERATOR_UUID},{PARTNER_ID}")

    result = trove_capture(event, gateway=fake_gateway, session_store=None)
    assert result is None

    await asyncio.sleep(0)

    # Notification sent to the operator (reverse direction)
    assert len(fake_adapter.sent) == 1
    dest, text = fake_adapter.sent[0]
    assert dest == OPERATOR_UUID
    assert "Sam added a household memory" in text

    assert _count_nuggets(capture_db) == 1


# ── T39: Derived partner routing from TROVE_PEOPLE (Phase 3) ────────


async def test_T39_derived_partner_routing(capture_db, monkeypatch):
    """T39: TROVE_PARTNERS unset → partner routing is DERIVED from
    TROVE_PEOPLE (the post-Phase-3 deploy shape): the fixture's
    two-person household derives a bidirectional map, so a partner DM
    notifies the operator and an operator DM notifies the partner.

    This is the test that fails if partner routing regresses to
    explicit-only, or if the derivation is key-level instead of
    person-level (a person's phone key self-mapped to their own UUID).
    """
    monkeypatch.delenv("TROVE_PARTNERS", raising=False)
    # The fixture's TROVE_PEOPLE already models the 2-person household:
    # operator (phone + UUID) and partner (UUID).

    fake_adapter = FakeAdapter()
    fake_gateway = FakeGateway(adapter=fake_adapter)

    # Partner sends → operator notified (the live partner-host path).
    event = FakeMessageEvent(
        text="partner thought",
        source=FakeSessionSource(user_id=PARTNER_ID, user_name="Sam"),
    )
    result = trove_capture(event, gateway=fake_gateway, session_store=None)
    assert result is None
    await asyncio.sleep(0)
    assert len(fake_adapter.sent) == 1
    dest, text = fake_adapter.sent[0]
    assert dest == OPERATOR_UUID
    assert "Sam added a household memory" in text
    assert _count_nuggets(capture_db) == 1

    # Operator sends → partner notified (reverse direction).
    event2 = FakeMessageEvent(
        text="operator thought",
        source=FakeSessionSource(user_id=OPERATOR_UUID, user_name="Alex"),
    )
    trove_capture(event2, gateway=fake_gateway, session_store=None)
    await asyncio.sleep(0)
    assert len(fake_adapter.sent) == 2
    dest, text = fake_adapter.sent[1]
    assert dest == PARTNER_ID
    assert "Alex added a household memory" in text
    assert _count_nuggets(capture_db) == 2


# ── T49: _partner_map derivation shape (Phase 3) ────────────────────


def test_T49_partner_map_person_level_derivation(monkeypatch):
    """T49: With TROVE_PARTNERS unset, _partner_map derives a
    PERSON-level map from TROVE_PEOPLE:
      - every key of a person maps to the first-listed OTHER person's
        preferred key — their UUID-shaped key when they have one (the
        reliable Signal-delivery shape), their phone key otherwise
        (deterministic; 2-person households get both directions);
      - the derivation is person-level, never key-level: a solo
        person's keys map to NO ONE (empty map, Q5 — no
        solo-specific code), and no key maps to the same person's
        other key (a key-level derivation would self-map the solo bot
        → echo-capture guard defeated);
      - TROVE_PARTNERS unset AND TROVE_PEOPLE unset → empty map.
    """
    monkeypatch.delenv("TROVE_PARTNERS", raising=False)

    # 2-person household, both with dual-format keys.
    monkeypatch.setenv(
        "TROVE_PEOPLE",
        f"{OPERATOR_ID}:Alex,{OPERATOR_UUID}:Alex,{PARTNER_UUID}:Sam",
    )
    m = _partner_map()
    # Every key maps to the other person's preferred (UUID) key.
    assert m[OPERATOR_UUID] == PARTNER_UUID  # the live partner-host path
    assert m[PARTNER_UUID] == OPERATOR_UUID  # partner's UUID → operator's UUID
    assert m[OPERATOR_ID] == PARTNER_UUID  # operator's phone → partner's UUID
    assert set(m.keys()) == {OPERATOR_UUID, PARTNER_UUID, OPERATOR_ID}
    assert set(m.values()) == {PARTNER_UUID, OPERATOR_UUID}

    # Partner with no UUID key → their phone key is the target.
    SAM_PHONE = "+131****0107"
    monkeypatch.setenv(
        "TROVE_PEOPLE", f"{OPERATOR_UUID}:Alex,{SAM_PHONE}:Sam"
    )
    assert _partner_map()[OPERATOR_UUID] == SAM_PHONE

    # Solo: 1 person → empty map (the echo-guard case the design-gap
    # fix exists for).
    monkeypatch.setenv("TROVE_PEOPLE", f"{OPERATOR_ID}:Alex,{OPERATOR_UUID}:Alex")
    assert _partner_map() == {}

    # 3-person household: every key → the other person's preferred key,
    # no key maps to the same person's other key (no self-mapping).
    OTHER_ID = "00000000-0000-4000-8000-000000000003"
    OTHER_PHONE = "+131****0108"
    monkeypatch.setenv(
        "TROVE_PEOPLE",
        f"{OPERATOR_ID}:Alex,{OPERATOR_UUID}:Alex,"
        f"{PARTNER_UUID}:Sam,{OTHER_ID}:Robin,{OTHER_PHONE}:Robin",
    )
    m = _partner_map()
    assert m[OPERATOR_ID] == PARTNER_UUID  # Alex's phone → Sam's UUID
    assert m[OPERATOR_UUID] == PARTNER_UUID
    assert m[OTHER_ID] == OPERATOR_UUID  # Robin's UUID → Alex's UUID
    assert m[OTHER_PHONE] == OPERATOR_UUID
    for k, v in m.items():
        assert k != v, f"key-level self-mapping: {k} → {v}"

    # Both unset → empty map.
    monkeypatch.delenv("TROVE_PEOPLE", raising=False)
    assert _partner_map() == {}


def test_T49_explicit_trove_partners_is_strict(monkeypatch):
    """T49 (explicit): When TROVE_PARTNERS IS set (non-blank), it is the
    ONLY routing source — TROVE_PEOPLE does not widen it (mirrors the
    TROVE_CAPTURE_SENDERS strict-override semantics from Phase 2). A
    blank TROVE_PARTNERS is treated as unset (derivation applies).
    """
    monkeypatch.setenv(
        "TROVE_PEOPLE",
        f"{OPERATOR_ID}:Alex,{OPERATOR_UUID}:Alex,{PARTNER_UUID}:Sam",
    )
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_UUID}")
    m = _partner_map()
    # Only the explicit pair — the operator's phone key is NOT routed
    # (it would be on the derived path).
    assert m == {OPERATOR_UUID: PARTNER_UUID, PARTNER_UUID: OPERATOR_UUID}

    # Blank value → unset-equivalent → derived path.
    monkeypatch.setenv("TROVE_PARTNERS", "   ")
    assert _partner_map()[OPERATOR_ID] == PARTNER_UUID


# ── T50: partner lookup falls back to user_id_alt (Phase 3) ─────────


async def test_T50_partner_notification_via_user_id_alt(capture_db, monkeypatch):
    """T50: An alt-only event (user_id=None, user_id_alt=UUID — the T31b
    shape, allowed through the capture gate by the same fallback) gets
    a partner notification too: _notify_partner looks the sender up by
    user_id, then user_id_alt (same bug class as the Phase 1
    _resolve_author alt fallback — pre-fix, alt-only events were
    captured but silently un-notified).
    """
    monkeypatch.setenv("TROVE_PARTNERS", f"{OPERATOR_UUID}:{PARTNER_ID}")
    monkeypatch.setenv("TROVE_CAPTURE_SENDERS", f"{OPERATOR_UUID}")

    fake_adapter = FakeAdapter()
    fake_gateway = FakeGateway(adapter=fake_adapter)

    event = FakeMessageEvent(
        text="alt-only sender thought",
        source=FakeSessionSource(user_id=None, user_id_alt=OPERATOR_UUID, user_name="Alex"),
    )

    result = trove_capture(event, gateway=fake_gateway, session_store=None)
    assert result is None
    await asyncio.sleep(0)

    assert len(fake_adapter.sent) == 1
    dest, text = fake_adapter.sent[0]
    assert dest == PARTNER_ID
    assert "Alex added a household memory" in text

    # The row's author is the alt ID (Phase 1 _resolve_author fallback).
    rows = _get_nuggets(capture_db)
    assert len(rows) == 1
    assert rows[0]["author"] == OPERATOR_UUID
