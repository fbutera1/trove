"""Unit tests for the Trove schema.

Covers T24, T25, T27, T28, T6 (schema-guard portion).
"""

import json
import sqlite3
import time
from pathlib import Path

import pytest

import trove.schema
from trove.db import connect, write_transaction
from trove.schema import (
    SCHEMA_VERSION,
    NUGGETS_V2_COLUMNS,
    generate_message_id,
    init_db,
    migrate,
    _MIGRATIONS,
)


# ── T24: init_db creates trove.db with correct schema ────────────────


class TestT24_InitDb:
    """T24 — init_db creates trove.db with the nuggets schema,
    nuggets_fts, and schema_version."""

    def test_nuggets_table_exists(self, trove_db):
        """The nuggets table exists with all expected columns."""
        rows = trove_db.execute(
            "PRAGMA table_info(nuggets)"
        ).fetchall()
        column_names = {r[1] for r in rows}
        expected = {
            "message_id", "created_at", "updated_at", "author", "source",
            "raw_content", "status", "classification", "entities", "summary",
            "confidence", "links", "metadata",
            "due_at", "assignee",
        }
        assert column_names == expected, f"Missing columns: {expected - column_names}"

    def test_nuggets_constraints(self, trove_db):
        """NOT NULL and DEFAULT constraints on key columns."""
        info = {r[1]: r for r in trove_db.execute(
            "PRAGMA table_info(nuggets)"
        ).fetchall()}
        # message_id is TEXT PRIMARY KEY
        assert info["message_id"][2] == "TEXT"
        assert info["message_id"][5] == 1  # pk
        # raw_content NOT NULL
        assert info["raw_content"][3] == 1  # notnull
        # source NOT NULL
        assert info["source"][3] == 1
        # status NOT NULL, DEFAULT 'captured'
        assert info["status"][3] == 1
        assert info["status"][4] == "'captured'"

    def test_nuggets_fts_exists(self, trove_db):
        """The nuggets_fts FTS5 virtual table exists."""
        row = trove_db.execute(
            "SELECT type, name FROM sqlite_master WHERE type='table' AND name='nuggets_fts'"
        ).fetchone()
        assert row is not None
        assert row[0] == "table"

    def test_schema_version_exists(self, trove_db):
        """schema_version table exists and records SCHEMA_VERSION."""
        row = trove_db.execute(
            "SELECT version FROM schema_version WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

    def test_init_db_creates_schema(self, tmp_path):
        """init_db() creates the DB with correct schema on a fresh path."""
        db_file = tmp_path / "init_test.db"
        conn = init_db(db_file)
        # Verify schema_version is set
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        assert row[0] == SCHEMA_VERSION
        # Verify nuggets table exists
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nuggets'"
        ).fetchone()
        assert row is not None
        conn.close()


# ── T25: Migration runner is idempotent ──────────────────────────────


class TestT25_Idempotent:
    """T25 — Running init_db twice on the same path is a no-op."""

    def test_double_init_is_noop(self, db_path):
        """Second init_db is a no-op: no error, schema_version unchanged."""
        conn1 = init_db(db_path)
        # Insert a row to verify no data loss
        with write_transaction(conn1):
            conn1.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                "VALUES (?, ?, ?, ?, ?)",
                ("test-id", time.time(), time.time(), "signal", "hello"),
            )
        first_version_time = conn1.execute(
            "SELECT applied_at FROM schema_version"
        ).fetchone()[0]
        conn1.close()

        # Re-init
        conn2 = init_db(db_path)
        second_version_time = conn2.execute(
            "SELECT applied_at FROM schema_version"
        ).fetchone()[0]
        # schema_version should be unchanged (INSERT OR REPLACE but same version)
        assert second_version_time == first_version_time
        # Row should still be there
        row = conn2.execute("SELECT message_id FROM nuggets WHERE message_id = ?",
                            ("test-id",)).fetchone()
        assert row is not None
        conn2.close()


# ── T27: Round-trip insert/select ────────────────────────────────────


class TestT27_RoundTrip:
    """T27 — A Nugget inserted with all fields round-trips with exact equality."""

    def test_full_round_trip(self, trove_db):
        """Insert a fully-populated Nugget, select back, assert exact equality."""
        msg_id = generate_message_id()
        now = time.time()
        entities = ["Alice", "Project X", "deadline"]
        links = ["other-nugget-1", "other-nugget-2"]
        metadata = {"model": "test-model", "attempts": 1}

        with write_transaction(trove_db):
            trove_db.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, author, "
                "source, raw_content, status, classification, entities, summary, "
                "confidence, links, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg_id,
                    now,
                    now,
                    "00000000-0000-4000-8000-0000000000aa",
                    "signal",
                    "Remember to call Alice about Project X",
                    "captured",
                    "task",
                    json.dumps(entities),
                    "Call Alice re: Project X",
                    0.95,
                    json.dumps(links),
                    json.dumps(metadata),
                ),
            )

        row = trove_db.execute(
            "SELECT message_id, created_at, updated_at, author, source, "
            "raw_content, status, classification, entities, summary, "
            "confidence, links, metadata "
            "FROM nuggets WHERE message_id = ?",
            (msg_id,),
        ).fetchone()

        assert row[0] == msg_id
        assert row[1] == now
        assert row[2] == now
        assert row[3] == "00000000-0000-4000-8000-0000000000aa"
        assert row[4] == "signal"
        assert row[5] == "Remember to call Alice about Project X"
        assert row[6] == "captured"
        assert row[7] == "task"
        assert json.loads(row[8]) == entities
        assert row[9] == "Call Alice re: Project X"
        assert row[10] == 0.95
        assert json.loads(row[11]) == links
        assert json.loads(row[12]) == metadata

    def test_minimal_round_trip(self, trove_db):
        """Insert a minimal Nugget (required fields only), select back."""
        msg_id = generate_message_id()
        now = time.time()

        with write_transaction(trove_db):
            trove_db.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                "VALUES (?, ?, ?, ?, ?)",
                (msg_id, now, now, "signal", "minimal nugget"),
            )

        row = trove_db.execute(
            "SELECT message_id, raw_content, status FROM nuggets WHERE message_id = ?",
            (msg_id,),
        ).fetchone()

        assert row[0] == msg_id
        assert row[1] == "minimal nugget"
        assert row[2] == "captured"  # DEFAULT


# ── T28: FTS sync ────────────────────────────────────────────────────


class TestT28_FTS:
    """T28 — nuggets_fts stays in sync with nuggets after insert and after enrich."""

    def test_fts_after_insert(self, trove_db):
        """After insert, FTS finds the row by raw_content."""
        msg_id = generate_message_id()
        with write_transaction(trove_db):
            trove_db.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content, summary, entities) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg_id, time.time(), time.time(), "signal",
                 "Meeting with Bob about the budget", None, None),
            )

        # Search by raw_content via FTS
        rows = trove_db.execute(
            "SELECT message_id FROM nuggets_fts WHERE nuggets_fts MATCH 'budget'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == msg_id

    def test_fts_after_enrich_update(self, trove_db):
        """After enrich-style UPDATE of summary+entities, FTS finds new terms."""
        msg_id = generate_message_id()
        with write_transaction(trove_db):
            trove_db.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                "VALUES (?, ?, ?, ?, ?)",
                (msg_id, time.time(), time.time(), "signal",
                 "Need to follow up on the Q3 report"),
            )

        # FTS finds raw_content term
        rows = trove_db.execute(
            "SELECT message_id FROM nuggets_fts WHERE nuggets_fts MATCH 'Q3'"
        ).fetchall()
        assert len(rows) == 1

        # Simulate enrichment: UPDATE summary + entities
        with write_transaction(trove_db):
            trove_db.execute(
                "UPDATE nuggets SET summary = ?, entities = ?, status = ? "
                "WHERE message_id = ?",
                ("Follow up on quarterly financial report",
                 json.dumps(["Q3", "financial report", "quarterly"]),
                 "enriched",
                 msg_id),
            )

        # FTS finds new summary term
        rows = trove_db.execute(
            "SELECT message_id FROM nuggets_fts WHERE nuggets_fts MATCH 'financial'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == msg_id

        # FTS finds new entities term
        rows = trove_db.execute(
            "SELECT message_id FROM nuggets_fts WHERE nuggets_fts MATCH 'quarterly'"
        ).fetchall()
        assert len(rows) == 1

    def test_generate_message_id_unique(self):
        """generate_message_id produces unique, sortable-ish ids."""
        ids = [generate_message_id() for _ in range(100)]
        assert len(ids) == len(set(ids)), "IDs should be unique"
        # All should be valid UUID strings
        import uuid
        for uid in ids:
            uuid.UUID(uid)  # Should not raise


# ── T6: raw_content immutability guard ───────────────────────────────


class TestT6_RawContentGuard:
    """T6 (schema-guard portion) — raw_content is write-once at the storage level."""

    def test_update_raw_content_aborts(self, trove_db):
        """Direct UPDATE of raw_content is rejected by the schema guard."""
        msg_id = generate_message_id()
        with write_transaction(trove_db):
            trove_db.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                "VALUES (?, ?, ?, ?, ?)",
                (msg_id, time.time(), time.time(), "signal", "original content"),
            )

        # Attempt to update raw_content — should raise
        with pytest.raises(sqlite3.IntegrityError, match="raw_content is immutable"):
            trove_db.execute(
                "UPDATE nuggets SET raw_content = ? WHERE message_id = ?",
                ("modified content", msg_id),
            )
            trove_db.commit()

        # Verify raw_content is unchanged
        row = trove_db.execute(
            "SELECT raw_content FROM nuggets WHERE message_id = ?",
            (msg_id,),
        ).fetchone()
        assert row[0] == "original content"

    def test_enrich_update_preserves_raw_content(self, trove_db):
        """An enrich-style UPDATE of AI fields does not change raw_content."""
        msg_id = generate_message_id()
        original = "Remember the meeting at 3pm"
        with write_transaction(trove_db):
            trove_db.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                "VALUES (?, ?, ?, ?, ?)",
                (msg_id, time.time(), time.time(), "signal", original),
            )

        # Enrich: update summary, entities, status, confidence
        with write_transaction(trove_db):
            trove_db.execute(
                "UPDATE nuggets SET summary = ?, entities = ?, status = ?, confidence = ? "
                "WHERE message_id = ?",
                ("Meeting reminder", json.dumps(["meeting", "3pm"]), "enriched", 0.9, msg_id),
            )

        # raw_content must be unchanged
        row = trove_db.execute(
            "SELECT raw_content FROM nuggets WHERE message_id = ?",
            (msg_id,),
        ).fetchone()
        assert row[0] == original


# ── T37: Schema v2 migration ─────────────────────────────────────────


class TestT37_SchemaV2:
    """T37 — v2 migration adds nullable due_at + assignee;
    a v1 DB upgrades to v2 with rows intact; re-init is a no-op."""

    def test_fresh_db_has_v2_columns(self, trove_db):
        """A freshly initialized DB has due_at (REAL) and assignee (TEXT), both nullable."""
        rows = trove_db.execute(
            "PRAGMA table_info(nuggets)"
        ).fetchall()
        columns = {r[1]: r for r in rows}
        assert "due_at" in columns, "due_at column missing"
        assert "assignee" in columns, "assignee column missing"
        # due_at is REAL
        assert columns["due_at"][2] == "REAL"
        # assignee is TEXT
        assert columns["assignee"][2] == "TEXT"
        # Both are nullable (notnull == 0)
        assert columns["due_at"][3] == 0, "due_at should be nullable"
        assert columns["assignee"][3] == 0, "assignee should be nullable"

    def test_v1_upgrade_preserves_rows(self, db_path):
        """A v1 DB (version 1 only) upgrades to v2 with existing rows intact."""
        # Build a v1 DB manually: apply only migration 1
        conn = connect(db_path)
        conn.executescript(
            trove.schema.NUGGETS_TABLE
            + trove.schema.SCHEMA_VERSION_TABLE
            + trove.schema.NUGGETS_FTS
            + trove.schema.TRIGGER_AFTER_INSERT
            + trove.schema.TRIGGER_AFTER_UPDATE
            + trove.schema.TRIGGER_RAW_CONTENT_GUARD
        )
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (1, time.time()),
        )
        conn.commit()

        # Insert a sample nugget
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                "VALUES (?, ?, ?, ?, ?)",
                ("v1-sample", time.time(), time.time(), "signal", "v1 row"),
            )
        conn.close()

        # Now run init_db (which calls migrate) — should apply migration 2
        conn2 = init_db(db_path)

        # Verify the sample row survives with due_at/assignee NULL
        row = conn2.execute(
            "SELECT message_id, raw_content, due_at, assignee FROM nuggets WHERE message_id = ?",
            ("v1-sample",),
        ).fetchone()
        assert row is not None, "v1 row was lost during upgrade"
        assert row[0] == "v1-sample"
        assert row[1] == "v1 row"
        assert row[2] is None  # due_at is NULL
        assert row[3] is None  # assignee is NULL

        # Verify schema_version has versions 1, 2, 3, and 4
        versions = {r[0] for r in conn2.execute(
            "SELECT version FROM schema_version"
        ).fetchall()}
        assert 1 in versions, "Migration 1 version row missing"
        assert 2 in versions, "Migration 2 version row missing"
        assert 3 in versions, "Migration 3 version row missing"
        assert 4 in versions, "Migration 4 version row missing"
        conn2.close()

    def test_reinit_is_noop(self, db_path):
        """Re-running init_db on a v2 DB is a no-op (ALTER not re-applied)."""
        # Initialize to v2
        conn1 = init_db(db_path)
        conn1.close()

        # Re-init — should not raise (ALTER TABLE ADD COLUMN is NOT idempotent)
        conn2 = init_db(db_path)

        # Verify schema_version still has versions 1, 2, 3, 4, no duplicates
        versions = [r[0] for r in conn2.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()]
        assert versions == [1, 2, 3, 4], f"Expected [1, 2, 3, 4], got {versions}"

        # Verify columns still exist (no duplicate column error)
        columns = {r[1] for r in conn2.execute(
            "PRAGMA table_info(nuggets)"
        ).fetchall()}
        assert "due_at" in columns
        assert "assignee" in columns
        conn2.close()


# ── T44: Schema v4 author migration ──────────────────────────────────


def _build_v3_db(db_path, rows):
    """Build a v3 DB (versions 1-3 applied) with the given (message_id, author) rows.

    Simulates a pre-v4 deployment: the composite author shapes that
    capture produced in the wild (UUID-in-parens on the partner host,
    phone-in-parens on the solo host).
    """
    conn = connect(db_path)
    conn.executescript(
        trove.schema.NUGGETS_TABLE
        + trove.schema.SCHEMA_VERSION_TABLE
        + trove.schema.NUGGETS_FTS
        + trove.schema.TRIGGER_AFTER_INSERT
        + trove.schema.TRIGGER_AFTER_UPDATE
        + trove.schema.TRIGGER_RAW_CONTENT_GUARD
    )
    conn.executescript(trove.schema.NUGGETS_V2_COLUMNS)
    for version in (1, 2, 3):
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time()),
        )
    for message_id, author in rows:
        conn.execute(
            "INSERT INTO nuggets (message_id, created_at, updated_at, author, source, raw_content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, time.time(), time.time(), author, "signal", "thought"),
        )
    conn.commit()
    conn.close()


class TestT44_SchemaV4Author:
    """T44 — v4 migration rewrites composite author values
    ("Name (id)") to the bare sender ID; bare IDs and NULLs are untouched;
    the migration is version-gated and a no-op on fresh DBs."""

    def test_composite_uuid_rows_migrated(self, db_path):
        """Partner-host shape: 'Frank (uuid)' → bare uuid."""
        _build_v3_db(
            db_path,
            [
                ("m-uuid", "Frank (00000000-0000-4000-8000-000000000001)"),
            ],
        )
        conn = init_db(db_path)
        author = conn.execute(
            "SELECT author FROM nuggets WHERE message_id = 'm-uuid'"
        ).fetchone()[0]
        assert author == "00000000-0000-4000-8000-000000000001"
        version = conn.execute(
            "SELECT version FROM schema_version WHERE version = 4"
        ).fetchone()
        assert version is not None
        conn.close()

    def test_composite_phone_rows_migrated(self, db_path):
        """Solo-host shape: 'chitown (phone)' → bare phone."""
        _build_v3_db(
            db_path,
            [
                ("m-phone", "chitown (+131****0973)"),
            ],
        )
        conn = init_db(db_path)
        author = conn.execute(
            "SELECT author FROM nuggets WHERE message_id = 'm-phone'"
        ).fetchone()[0]
        assert author == "+131****0973"
        conn.close()

    def test_bare_ids_and_nulls_untouched(self, db_path):
        """Rows already in the post-v4 shape (bare phone/UUID) and NULL
        authors are left as-is."""
        _build_v3_db(
            db_path,
            [
                ("m-bare-phone", "+131****0100"),
                ("m-bare-uuid", "00000000-0000-4000-8000-000000000002"),
                ("m-null", None),
                ("m-composite", "Sam (00000000-0000-4000-8000-000000000003)"),
            ],
        )
        conn = init_db(db_path)
        authors = dict(
            conn.execute(
                "SELECT message_id, author FROM nuggets"
            ).fetchall()
        )
        assert authors["m-bare-phone"] == "+131****0100"
        assert authors["m-bare-uuid"] == "00000000-0000-4000-8000-000000000002"
        assert authors["m-null"] is None
        assert authors["m-composite"] == "00000000-0000-4000-8000-000000000003"
        conn.close()

    def test_fresh_db_is_noop(self, db_path):
        """A fresh (v4) DB has zero composite rows — the migration is a
        no-op; re-init stays a no-op (version-gated)."""
        conn1 = init_db(db_path)
        conn1.close()
        conn2 = init_db(db_path)
        versions = [r[0] for r in conn2.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()]
        assert versions == [1, 2, 3, 4]
        count = conn2.execute("SELECT COUNT(*) FROM nuggets").fetchone()[0]
        assert count == 0
        conn2.close()
