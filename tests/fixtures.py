"""Shared test fixtures for Trove.

Importable by test modules (unlike conftest.py which is pytest-discovered only).
"""

import sqlite3
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

from trove.db import connect as db_connect
from trove.schema import generate_message_id

# ── Operator identity constants (clearly-fake placeholders, not real) ─
#
# Model the partner world: inbound Signal DMs carry the sender's UUID
# as user_id (see README "Signal UUID requirements"). OPERATOR_UUID is
# the operator's UUID sender ID; OPERATOR_ID is the same person's phone
# (the note-to-self / solo shape, where user_id is the phone number).
# Both are obviously fake (all-zero UUIDs / sequential phones).

OPERATOR_ID = "+131****0100"
OPERATOR_UUID = "00000000-0000-4000-8000-000000000001"
PARTNER_UUID = "00000000-0000-4000-8000-000000000002"


@dataclass
class FakeSessionSource:
    """Mimic the Hermes SessionSource attributes the capture filter reads."""
    platform: str = "signal"
    chat_type: str = "dm"
    chat_id: Optional[str] = None
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    user_id_alt: Optional[str] = None
    chat_name: Optional[str] = None
    is_bot: bool = False


@dataclass
class FakeAdapter:
    """Fake Hermes messaging adapter for partner-notification tests.

    Records all send() calls and supports configurable success/failure.
    """
    sent: list = field(default_factory=list)
    success: bool = True
    raise_exc: Exception = None
    _recipient_uuid_by_number: dict = field(default_factory=dict)

    async def send(self, dest: str, text: str):
        """Record the send call and return (or raise) based on config.

        Returns a SendResult-shaped object (SimpleNamespace with .success
        attribute) so the production done-callback's
        ``getattr(result, "success", True)`` behaves identically to the
        real Hermes ``gateway.platforms.base.SendResult`` dataclass.
        """
        self.sent.append((dest, text))
        if self.raise_exc is not None:
            raise self.raise_exc
        return SimpleNamespace(
            success=self.success,
            error=None if self.success else "fake error",
            message_id=None,
        )


@dataclass
class FakeGateway:
    """Fake Hermes GatewayRunner for partner-notification tests.

    Holds a FakeAdapter and tracks _adapter_for_source calls.
    """
    adapter: FakeAdapter = field(default_factory=FakeAdapter)
    resolved: list = field(default_factory=list)

    def _adapter_for_source(self, source):
        """Return the held adapter, recording the call."""
        self.resolved.append(source)
        return self.adapter


@dataclass
class FakeMessageEvent:
    """Lightweight fake MessageEvent for testing the capture hook.

    Mimics the Hermes MessageEvent shape expected by trove_capture.

    Default source models the partner world: inbound Signal DMs carry
    the sender's UUID as user_id (user_name is the Signal profile name,
    which capture must NOT bake into the author column — post-v4 the
    author is the raw sender ID, resolved to a display name at read
    time via TROVE_PEOPLE).
    """
    text: str
    source: FakeSessionSource = field(
        default_factory=lambda: FakeSessionSource(
            user_id=OPERATOR_UUID,
            user_name="Alex",
            chat_id=OPERATOR_UUID,
            chat_name="Alex",
        )
    )
    message_id: str = None
    internal: bool = False
    raw_message: dict = field(default_factory=lambda: {"timestamp_ms": 1700000000000})


# ── Shared DB helpers ────────────────────────────────────────────────


def _get_nuggets(db_path):
    """Return all nuggets rows as dicts."""
    conn = db_connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM nuggets").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _count_nuggets(db_path):
    """Return the number of nuggets rows."""
    conn = db_connect(db_path)
    row = conn.execute("SELECT COUNT(*) FROM nuggets").fetchone()
    conn.close()
    return row[0]


def insert_captured_nugget(
    db_path,
    message_id=None,
    text="need to reorder lobster clasps",
    author=None,
):
    """Insert a status='captured' row with a known message_id.

    Creates a realistic captured Nugget row so enrichment tests start
    from a known state (as if trove_capture already ran).

    Args:
        db_path: Path to the initialized trove.db.
        message_id: Trove-generated id. Auto-generated if None.
        text: The raw_content text.
        author: Raw sender ID for the author column. Defaults to
            OPERATOR_UUID (the partner-world sender ID capture stores
            post-v4 — raw ID, not a "Name (id)" composite).

    Returns:
        The message_id of the inserted row.
    """
    if message_id is None:
        message_id = generate_message_id()
    if author is None:
        author = OPERATOR_UUID
    now = time.time()
    conn = db_connect(db_path)
    conn.execute(
        """INSERT INTO nuggets
           (message_id, created_at, updated_at, author, source,
            raw_content, status, classification, entities, summary,
            confidence, links, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            now,
            now,
            author,
            "signal",
            text,
            "captured",
            None, None, None,
            None, None, None,
        ),
    )
    conn.commit()
    conn.close()
    return message_id
