"""Capture hook for Trove.

Implements the `pre_gateway_dispatch` hook that persists every inbound
Signal message as a Nugget *before* the agent turn runs — the ordering
invariant behind the "Trove never forgets" promise.

Deterministic, LLM-free, and synchronous.
Never blocks dispatch — always returns None (allow).

Sender-allowlist gate: before the insert, check the sender against a
resolved allowlist with the fallback chain `TROVE_CAPTURE_SENDERS`
(explicit override) → `TROVE_PEOPLE` keys (derived) →
`SIGNAL_ALLOWED_USERS` (Hermes fallback). Non-allowlisted sender →
write no Nugget. No resolvable allowlist or `*` open mode → deny all
(fail-safe) with a throttled WARNING.

Partner routing: `TROVE_PARTNERS` (explicit) → derived from
`TROVE_PEOPLE` (person-level: each person's keys map to the
first-listed other person's UUID key — the household model; solo
derives an empty map).
"""

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, cast

from trove import db, schema
from trove.people import parse_people_env

logger = logging.getLogger(__name__)

# Module-level timestamp for throttling the "capture DISABLED" warning.
_disabled_warn_ts = 0.0
_DISABLED_WARN_INTERVAL = 60.0  # seconds

# Cached init state — avoids re-checking schema on every capture.
_db_init_done = False


def _parse_comma_list(raw: str | None) -> set:
    """Parse a comma-separated env-var value into a set of stripped strings.

    Mirrors Hermes' `_parse_comma_list` (`gateway/platforms/signal.py`):
    comma-split, strip, drop empties.
    """
    if not raw:
        return set()
    return {v.strip() for v in raw.split(",") if v.strip()}


def _capture_allowed(event) -> bool:
    """Check whether the sender of *event* is allowed to produce a Nugget.

    Fallback chain:
      1. `TROVE_CAPTURE_SENDERS` env var (explicit override — strict;
         when set, it is the ONLY capture allowlist)
      2. `TROVE_PEOPLE` keys (derived — the preferred path: the people
         whose messages should become Nuggets are exactly the people in
         the identity map, so their bot number / self-chat number is
         excluded automatically)
      3. `SIGNAL_ALLOWED_USERS` env var (Hermes legacy fallback)

    `*` (open mode) is treated as unset → deny all (fail-safe).
    Returns True only when the sender ID intersects the resolved allowlist.
    """
    raw = os.getenv("TROVE_CAPTURE_SENDERS")
    if raw is not None and raw.strip():
        allowed = _parse_comma_list(raw)
    else:
        allowed = set(parse_people_env()) or _parse_comma_list(
            os.getenv("SIGNAL_ALLOWED_USERS")
        )
    # `*` means "reply to everyone" for Hermes; we must NOT treat it as
    # "capture everyone" — treat it as empty → deny.
    if "*" in allowed:
        allowed = set()

    source = getattr(event, "source", None)
    sender_ids = {
        s for s in (
            getattr(source, "user_id", None) if source is not None else None,
            getattr(source, "user_id_alt", None) if source is not None else None,
        )
        if s
    }

    if not allowed:
        # Capture is disabled by config — emit a loud, throttled WARNING.
        _warn_capture_disabled()
        return False

    if not sender_ids:
        return False

    if allowed & sender_ids:
        return True

    # Sender not in allowlist — quiet DEBUG log.
    logger.debug(
        "trove capture: sender %s not in capture allowlist — skipping",
        sender_ids,
    )
    return False


def _warn_capture_disabled():
    """Emit a throttled WARNING when capture is disabled by config."""
    global _disabled_warn_ts
    now = time.monotonic()
    if now - _disabled_warn_ts >= _DISABLED_WARN_INTERVAL:
        _disabled_warn_ts = now
        logger.warning(
            "trove capture: DISABLED — set TROVE_PEOPLE (or "
            "TROVE_CAPTURE_SENDERS / SIGNAL_ALLOWED_USERS) in "
            "~/.hermes/.env or thoughts will be dropped"
        )


def _resolve_author(event) -> str:
    """Extract the raw sender identity from a MessageEvent.

    Returns the sender's raw identifier — `user_id`, or `user_id_alt`
    when `user_id` is absent (the capture gate treats the two as
    equivalent sender IDs). This is a programmatic identity stored in
    the `author` column; display names are resolved at read time via
    `TROVE_PEOPLE` (tools.nugget_tasks, dashboard), never baked in here.

    Note-to-self (solo) messages carry the sender's phone number as
    `user_id`; partner DMs carry a Signal UUID. Either shape is stored
    verbatim and resolved the same way.

    Falls back to chat_name / chat_id / str(source) only as a last
    resort (non-DM shapes; the allowlist gate guarantees a sender ID
    for every captured event).
    """
    source = getattr(event, "source", None)
    if source is None:
        return None
    # SessionSource has .user_id and .user_id_alt
    user_id = getattr(source, "user_id", None)
    if user_id:
        return str(user_id)
    user_id_alt = getattr(source, "user_id_alt", None)
    if user_id_alt:
        return str(user_id_alt)
    # Try chat_name / chat_id before the dataclass repr
    chat_name = getattr(source, "chat_name", None)
    if chat_name:
        return str(chat_name)
    chat_id = getattr(source, "chat_id", None)
    if chat_id:
        return str(chat_id)
    return str(source)


def _partner_map() -> dict:
    """Resolve sender-to-partner routing.

    Fallback chain:
      1. `TROVE_PARTNERS` env var (explicit — strict; when set, it is
         the ONLY routing source). The value is a comma-separated list
         of ``sender_id:partner_id`` pairs; each pair is bidirectional
         (``A:B`` produces both ``{A: B}`` and ``{B: A}``). Invalid
         pairs are ignored so a bad notification setting cannot block
         capture.
      2. `TROVE_PEOPLE` (derived — the preferred path): group the map's
         keys by display name (a person can hold several identifier
         keys — phone + UUID), then map every key of one person to the
         UUID-shaped key of each OTHER person (signal-cli delivers
         UUIDs reliably and rejects phone sends — `UNREGISTERED_FAILURE`).
         This is the household model: everyone notifies everyone else.
         Each person's keys map to the OTHER person's preferred key,
         with the first-listed other person winning the single-valued
         lookup — deterministic; a 3+ person household with subgroups
         is the explicit TROVE_PARTNERS case.
         Derivation is deliberately person-level, not key-level:
         pairing a person's phone key to the same person's UUID key
         would make a solo bot notify itself (defeating the
         echo-capture guard). Solo (1 person) derives an empty map → no
         notifications, no solo-specific code.

    **Use UUIDs** in `TROVE_PARTNERS` (not phone numbers) for reliable
    Signal delivery — see README. The derived path inherits that
    property: it always targets the other person's UUID-shaped key when
    one is present.
    """
    raw = os.getenv("TROVE_PARTNERS")
    if raw is not None and raw.strip():
        result = {}
        for pair in _parse_comma_list(raw):
            sender, separator, partner = pair.partition(":")
            if separator and sender.strip() and partner.strip():
                s = sender.strip()
                p = partner.strip()
                result[s] = p
                result[p] = s  # bidirectional
        return result

    # Derived path: person-level (see docstring), never self-mapped.
    # When a person holds several keys, the MAP's target prefers the
    # UUID-shaped key (signal-cli delivers UUIDs reliably and rejects
    # phone sends with UNREGISTERED_FAILURE) — the key a live partner
    # DM arrives under, so this is the key the partner lookup actually
    # needs.
    people = parse_people_env()
    by_name: dict[str, list[str]] = {}
    for sender_id, name in people.items():
        by_name.setdefault(name, []).append(sender_id)

    def _preferred(keys: list[str]) -> str:
        uuids = [k for k in keys if len(k) == 36 and k.count("-") == 4]
        return uuids[0] if uuids else keys[0]

    result = {}
    for name, keys in by_name.items():
        # The FIRST other person listed in TROVE_PEOPLE is this
        # person's derived partner — deterministic; 3+ person routing
        # with subgroups is the explicit TROVE_PARTNERS case.
        other = next(
            (o for o in by_name if o != name), None
        )
        if other is None:
            continue  # solo → no partners
        target = _preferred(by_name[other])
        for k in keys:
            result[k] = target  # every key → the partner's preferred key
    return result


def _notify_partner(event, gateway, raw_content: str, message_id: str) -> None:
    """Schedule a best-effort Signal notice for the other spouse.

    Capture remains synchronous and authoritative. Notification is deliberately
    scheduled after the database commit and is never allowed to raise into the
    gateway dispatch path.
    """
    if gateway is None:
        return

    source = getattr(event, "source", None)
    sender_id = getattr(source, "user_id", None) if source is not None else None
    if not sender_id:
        # Same sender-identity fallback as the capture gate / author
        # resolution (T31b shape: alt-only events exist in production).
        sender_id = getattr(source, "user_id_alt", None) if source is not None else None
    partner_id = _partner_map().get(sender_id)
    if not partner_id:
        return

    adapter_for_source = getattr(gateway, "_adapter_for_source", None)
    if not callable(adapter_for_source):
        logger.debug("trove notification skipped: gateway has no adapter resolver")
        return

    try:
        adapter = adapter_for_source(source)
        send = getattr(adapter, "send", None)
        if not callable(send):
            return
        author = getattr(source, "user_name", None) or sender_id or "Your spouse"
        excerpt = str(raw_content or "").strip()
        if len(excerpt) > 500:
            excerpt = excerpt[:497] + "..."
        message = f"{author} added a household memory:\n{excerpt}"
        task = asyncio.ensure_future(cast(Awaitable[Any], send(partner_id, message)))

        def _notification_done(done_task):
            try:
                result = done_task.result()
                if getattr(result, "success", True) is False:
                    logger.warning(
                        "trove notification failed: message_id=%s partner=%s error=%s",
                        message_id,
                        partner_id,
                        getattr(result, "error", "unknown error"),
                    )
            except Exception:
                logger.warning(
                    "trove notification failed: message_id=%s partner=%s",
                    message_id,
                    partner_id,
                    exc_info=True,
                )

        task.add_done_callback(_notification_done)
    except RuntimeError:
        # Unit tests and non-gateway callers may have no running event loop.
        logger.debug("trove notification skipped: no running event loop")
    except Exception:
        logger.warning(
            "trove notification setup failed: message_id=%s",
            message_id,
            exc_info=True,
        )


def trove_capture(event, gateway, session_store, **kwargs):
    """pre_gateway_dispatch hook. Synchronous.

    Args:
        event: MessageEvent with .text, .source, .message_id (None for Signal),
               .internal (bool), .raw_message (dict with timestamp_ms)
        gateway: GatewayRunner — used only for best-effort partner
                 notification (see `_notify_partner`); never used to
                 skip/rewrite dispatch. None outside a live gateway turn.
        session_store: SessionStore (not used by Trove)

    Returns:
        None (allow) — dispatch proceeds. Never skip or rewrite.
    """
    global _db_init_done

    # ── Skip internal/system events ──────────────────────────────────
    if getattr(event, "internal", False):
        return None

    # ── Sender-allowlist gate (capture-scope filter) ───────
    if not _capture_allowed(event):
        return None  # not a Trove thought; dispatch still proceeds

    # ── Read the envelope ────────────────────────────────────────────
    text = event.text
    sender = _resolve_author(event)
    raw_message = getattr(event, "raw_message", {}) or {}
    envelope_ts = raw_message.get("timestamp_ms")

    # ── Build the row ────────────────────────────────────────────────
    message_id = schema.generate_message_id()
    source = "signal"
    raw_content = text
    created_at = float(envelope_ts) / 1000.0 if envelope_ts is not None else time.time()
    updated_at = created_at
    author = sender
    status = "captured"

    # ── Insert into trove.db ─────────────────────────────────────────
    db_path = db.get_trove_db_path()

    # Defensive: _resolve_author should never return None here (the
    # allowlist gate already required a non-empty sender_id), but if it
    # does, log a warning rather than silently inserting a NULL author.
    if author is None:
        logger.warning("trove capture: resolved author is None for msg=%s", message_id)

    start_time = time.time()

    try:
        conn = db.connect(db_path)
        try:
            # Lazy init: ensure schema exists before first write.
            if not _db_init_done:
                schema.init_db(db_path)
                _db_init_done = True

            with db.write_transaction(conn):
                conn.execute(
                    """\
                    INSERT INTO nuggets (
                        message_id, created_at, updated_at,
                        author, source, raw_content, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        created_at,
                        updated_at,
                        author,
                        source,
                        raw_content,
                        status,
                    ),
                )

            # ── Log success ──────────────────────────────────────────
            latency_ms = (time.time() - start_time) * 1000
            logger.info(
                "trove capture: msg=%s latency_ms=%.1f",
                message_id,
                latency_ms,
            )
            _notify_partner(event, gateway, raw_content, message_id)
        finally:
            conn.close()

    except Exception as e:
        # ── Log failure and return None anyway ───────────────────────
        logger.error(
            "trove capture: failed to write msg=%s: %s",
            message_id,
            type(e).__name__,
            exc_info=True,
        )
        # Dispatch must proceed — the user still gets a reply.
        return None

    # ── Always return None (allow) ───────────────────────────────────
    return None
