"""Integration tests for Trove DB concurrency.

Covers T26 — WAL + BEGIN IMMEDIATE + busy-timeout/retry survives concurrent writers.
"""

import sqlite3
import threading
import time

from trove.db import connect, write_transaction
from trove.schema import generate_message_id, init_db


# ── T26: Concurrent writers ─────────────────────────────────────────


class TestT26_Concurrency:
    """T26 — Two concurrent writers against the same trove.db (WAL + BEGIN IMMEDIATE
    + busy-timeout/retry) both complete with no lost rows and no unhandled
    'database is locked'."""

    def test_concurrent_writers_no_lost_rows(self, db_path):
        """Two threads each insert rows; all writes land, no exceptions escape."""
        NUM_WRITERS = 2
        ROWS_PER_WRITER = 50

        # Initialize the DB
        init_db(db_path)

        errors = []
        writer_ids = []

        def writer(writer_id):
            """Each writer opens its own connection and inserts rows."""
            conn = connect(db_path)
            local_ids = []
            try:
                for i in range(ROWS_PER_WRITER):
                    msg_id = generate_message_id()
                    with write_transaction(conn):
                        conn.execute(
                            "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (msg_id, time.time(), time.time(), "signal",
                             f"writer-{writer_id} row-{i}"),
                        )
                    local_ids.append(msg_id)
            except Exception as e:
                errors.append((writer_id, e))
            finally:
                conn.close()
            writer_ids.extend(local_ids)

        # Launch two writer threads
        threads = [
            threading.Thread(target=writer, args=(0,)),
            threading.Thread(target=writer, args=(1,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Assert no errors
        assert len(errors) == 0, f"Writer errors: {errors}"

        # Assert all rows are present
        reader = connect(db_path)
        total = reader.execute("SELECT COUNT(*) FROM nuggets").fetchone()[0]
        assert total == NUM_WRITERS * ROWS_PER_WRITER, \
            f"Expected {NUM_WRITERS * ROWS_PER_WRITER} rows, got {total}"

        # Assert all generated IDs are in the DB
        existing_ids = {r[0] for r in reader.execute(
            "SELECT message_id FROM nuggets"
        ).fetchall()}
        for wid in writer_ids:
            assert wid in existing_ids, f"Missing writer ID: {wid}"

        reader.close()

    def test_concurrent_writers_wal_mode(self, db_path):
        """Verify WAL mode is active and concurrent reads work during writes."""
        init_db(db_path)

        # Verify WAL mode
        conn = connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"Expected WAL mode, got {mode}"

        # Insert a row
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                "VALUES (?, ?, ?, ?, ?)",
                (generate_message_id(), time.time(), time.time(), "signal", "wal test"),
            )

        # Reader thread
        read_errors = []
        read_count = [0]

        def reader():
            rconn = connect(db_path)
            try:
                for _ in range(20):
                    count = rconn.execute("SELECT COUNT(*) FROM nuggets").fetchone()[0]
                    read_count[0] = count
                    time.sleep(0.01)
            except Exception as e:
                read_errors.append(e)
            finally:
                rconn.close()

        # Start reader, then write concurrently
        t = threading.Thread(target=reader)
        t.start()
        for i in range(10):
            with write_transaction(conn):
                conn.execute(
                    "INSERT INTO nuggets (message_id, created_at, updated_at, source, raw_content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (generate_message_id(), time.time(), time.time(), "signal", f"wal row {i}"),
                )
            time.sleep(0.01)
        t.join(timeout=10)

        assert len(read_errors) == 0, f"Reader errors: {read_errors}"
        assert read_count[0] == 11, f"Expected 11 rows, reader saw {read_count[0]}"
        conn.close()
