"""Schema definitions for the Trove database.

Owns the `nuggets` table DDL, FTS virtual table setup, sync triggers,
the migration runner, and the Trove id generator.

Design notes:
  - `raw_content` lives on the Nugget (write-once, immutable).
  - `message_id` is Trove-generated (uuid7).
"""

import os
import re
import time
import uuid
from pathlib import Path

from trove.db import connect, write_transaction, get_trove_db_path

SCHEMA_VERSION = 4

# ── DDL ──────────────────────────────────────────────────────────────

NUGGETS_TABLE = """\
CREATE TABLE IF NOT EXISTS nuggets (
    message_id        TEXT PRIMARY KEY,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    author            TEXT,
    source            TEXT NOT NULL,
    raw_content       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'captured',
    classification    TEXT,
    entities          TEXT,
    summary           TEXT,
    confidence        REAL,
    links             TEXT,
    metadata          TEXT
);
"""

SCHEMA_VERSION_TABLE = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL
);
"""

NUGGETS_FTS = """\
CREATE VIRTUAL TABLE IF NOT EXISTS nuggets_fts USING fts5(
    message_id,
    raw_content,
    summary,
    entities
);
"""

# Sync triggers — keep nuggets_fts in sync with nuggets.
# AFTER INSERT: index the new row.
# AFTER UPDATE OF summary, entities: re-index when AI fields change.
# NOTE: No trigger for raw_content UPDATE — it is write-once (enforced by
# the raw_content_guard trigger below).

TRIGGER_AFTER_INSERT = """\
CREATE TRIGGER IF NOT EXISTS nuggets_fts_insert
AFTER INSERT ON nuggets
BEGIN
    INSERT INTO nuggets_fts(message_id, raw_content, summary, entities)
    VALUES (new.message_id, new.raw_content, new.summary, new.entities);
END;
"""

TRIGGER_AFTER_UPDATE = """\
CREATE TRIGGER IF NOT EXISTS nuggets_fts_update
AFTER UPDATE OF summary, entities ON nuggets
BEGIN
    DELETE FROM nuggets_fts WHERE message_id = old.message_id;
    INSERT INTO nuggets_fts(message_id, raw_content, summary, entities)
    VALUES (new.message_id, new.raw_content, new.summary, new.entities);
END;
"""

# Guard: prevent any UPDATE of raw_content (write-once immutability).
TRIGGER_RAW_CONTENT_GUARD = """\
CREATE TRIGGER IF NOT EXISTS raw_content_guard
BEFORE UPDATE OF raw_content ON nuggets
BEGIN
    SELECT RAISE(ABORT, 'raw_content is immutable');
END;
"""

# ── Id generation ────────────────────────────────────────────────────


def generate_message_id() -> str:
    """Generate a timestamp-ordered unique id as a string.

    Uses uuid.uuid7() when available (Python 3.14+). Falls back to a
    custom timestamp-ordered UUID for Python < 3.14. The fallback is not
    a strict RFC 9562 implementation — it packs a 48-bit millisecond
    timestamp with random bits but differs in bit allocation and uses
    os.urandom for entropy.

    Pure function — no DB, no I/O.

    Returns:
        A UUID string (e.g. '0194...').
    """
    try:
        # Python 3.14+ has uuid.uuid7()
        return str(uuid.uuid7())
    except AttributeError:
        # Custom timestamp-ordered UUID for Python < 3.14 (3.11–3.13)
        # Layout: [48b ts][4b ver=0111][12b rand_a][2b variant=10][62b rand_b]
        timestamp_ms = time.time_ns() // 1_000_000
        rand_int = int.from_bytes(os.urandom(8), 'big')
        ts_part = timestamp_ms & 0xFFFFFFFFFFFF  # 48 bits
        upper_64 = (ts_part << 16) | 0x7000 | ((rand_int >> 48) & 0xFFF)
        lower_64 = 0x8000000000000000 | (rand_int & 0x7FFFFFFFFFFFFFFF)
        uuid_bytes = upper_64.to_bytes(8, 'big') + lower_64.to_bytes(8, 'big')
        return str(uuid.UUID(bytes=uuid_bytes))


# ── V2 migration columns ─────────────────────────────────────────────
#
# These ALTER TABLE statements are NOT idempotent (re-running would fail with
# "duplicate column name"). The version gate in migrate() is load-bearing.

NUGGETS_V2_COLUMNS = """\
ALTER TABLE nuggets ADD COLUMN due_at REAL;
ALTER TABLE nuggets ADD COLUMN assignee TEXT;
"""

# ── V3 migration: drop dead columns ──────────────────────────────────
#
# embedding, hermes_message_id, hermes_session_id were never written by
# capture or tools. Drop them from existing DBs. Safe because no code reads
# or writes these columns (only tests inserted NULLs, which are also fixed).
# ALTER TABLE DROP COLUMN requires SQLite >= 3.35.0.
#
# NOTE: This migration is conditional — it only drops columns that exist.
# A v1 DB created from the current DDL (which no longer includes these
# columns) would fail an unconditional DROP. The migrate() runner handles
# this by checking column existence before each DROP.

NUGGETS_V3_DROP_COLUMNS = "DROP_DEAD_COLUMNS"  # sentinel — handled specially in migrate()

# ── V4 migration: normalize author to the raw sender ID ─────────────
#
# Pre-v4 capture stored a human-readable composite in `author`
# ("Name (user_id)") when both user_name and user_id were present.
# That made the column unmatchable by any ID-based lookup (nugget_tasks
# resolved names via TROVE_PEOPLE keys, which are raw IDs). v4 capture
# stores the raw sender ID instead; this migration rewrites existing
# composite rows to the bare ID inside the trailing parentheses.
#
# The regex matches both composite shapes in the wild:
#   "Frank (<uuid>)"        → partner host (UUID in parens)
#   "chitown (<phone>)"     → solo host (phone in parens)
# Rows that don't match (bare phone, bare UUID, NULL, no trailing parens)
# are left as-is — a bare ID is already in the post-v4 format.
#
# `author` has no guard trigger (only raw_content is protected), so the
# UPDATE is safe. NOT idempotent in the ADD/DROP sense — but re-running
# the UPDATE is a no-op on already-migrated rows (the WHERE clause only
# matches composite shapes), and the version gate prevents re-runs anyway.

_AUTHOR_COMPOSITE_RE = re.compile(r"^\s*[^()]+ \(([^()]+)\)\s*$")


def _normalize_author(conn):
    """Rewrite composite author values to the bare ID in parentheses.

    Runs inside the v4 migration transaction (caller manages it).
    Fetches all non-NULL authors; the regex (not a SQL pattern) decides
    which rows are composites, so no SQL GLOB subtleties are involved.
    """
    rows = conn.execute(
        "SELECT message_id, author FROM nuggets WHERE author IS NOT NULL"
    ).fetchall()
    for message_id, author in rows:
        m = _AUTHOR_COMPOSITE_RE.match(author)
        if m:
            conn.execute(
                "UPDATE nuggets SET author = ? WHERE message_id = ?",
                (m.group(1), message_id),
            )

# ── Migration runner ─────────────────────────────────────────────────

# Ordered migration definitions — each is a (version, sql) tuple.
# Migration 1 = base DDL (idempotent CREATE IF NOT EXISTS).
# Migration 2 = v2 columns (NOT idempotent — version gate is load-bearing).
# Migration 3 = drop dead columns (NOT idempotent — version gate is load-bearing).
# Migration 4 = normalize composite author values to bare sender IDs
# (UPDATE-only; safe to re-run, version-gated like the others).

_MIGRATIONS = [
    (
        1,
        NUGGETS_TABLE
        + SCHEMA_VERSION_TABLE
        + NUGGETS_FTS
        + TRIGGER_AFTER_INSERT
        + TRIGGER_AFTER_UPDATE
        + TRIGGER_RAW_CONTENT_GUARD,
    ),
    (2, NUGGETS_V2_COLUMNS),
    (3, NUGGETS_V3_DROP_COLUMNS),
    (4, "NORMALIZE_AUTHOR"),
]


_DEAD_COLUMNS = ("embedding", "hermes_message_id", "hermes_session_id")


def _drop_dead_columns(conn):
    """Drop columns that were never used by capture or tools.

    Conditional — only drops columns that actually exist. A fresh DB
    created from the current DDL won't have them, so the DROP is skipped.
    """
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(nuggets)").fetchall()
    }
    for col in _DEAD_COLUMNS:
        if col in existing:
            conn.execute(f"ALTER TABLE nuggets DROP COLUMN {col}")


def migrate(conn):
    """Run schema migrations forward to SCHEMA_VERSION.

    Uses versioned, gated migrations: each migration is applied only if its
    version is not yet recorded in `schema_version`. Each migration runs in
    its own transaction, and the version row is inserted after success.

    Migration 1 (base DDL) is idempotent (CREATE IF NOT EXISTS).
    Migration 2 (ALTER TABLE ADD COLUMN) is NOT idempotent — the version
    gate prevents duplicate-column errors on re-init.
    Migration 3 (ALTER TABLE DROP COLUMN) is NOT idempotent — the version
    gate prevents "no such column" errors on re-init.
    Migration 4 (author normalization UPDATE) is a no-op on already
    migrated rows, but is version-gated like the others.

    Args:
        conn: An open sqlite3.Connection (caller manages transactions).
    """
    # Ensure schema_version table exists (may not on very first run)
    conn.executescript(SCHEMA_VERSION_TABLE)

    # Read the set of applied versions
    applied = {
        row[0] for row in conn.execute(
            "SELECT version FROM schema_version"
        ).fetchall()
    }

    for version, sql in _MIGRATIONS:
        if version in applied:
            continue
        if version > SCHEMA_VERSION:
            continue

        if sql == "DROP_DEAD_COLUMNS":
            _drop_dead_columns(conn)
        elif sql == "NORMALIZE_AUTHOR":
            _normalize_author(conn)
        else:
            # Apply this migration in its own transaction
            conn.executescript(sql)

        # Record the version
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time()),
        )
        conn.commit()


def init_db(db_path=None):
    """Initialize (or migrate) the Trove database.

    Creates the DB file and runs migrations if needed. Idempotent.

    Args:
        db_path: Path to the database file. Defaults to the TROVE_DB
            env var (if set) or ~/.hermes/trove.db.

    Returns:
        An open sqlite3.Connection.
    """
    if db_path is None:
        db_path = get_trove_db_path()
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    return conn



