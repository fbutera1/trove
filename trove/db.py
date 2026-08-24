"""Shared SQLite helpers for Trove.

Provides database connection helpers including:
  - WAL mode configuration
  - BEGIN IMMEDIATE for write transactions
  - Busy-timeout + app-level retry logic

Trove owns `trove.db` and never writes `state.db`.
"""

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


DEFAULT_DB_PATH = Path.home() / ".hermes" / "trove.db"

# ── Path resolution (injectable for tests) ──────────────────────────

# Explicit programmatic override (set_trove_db_path); wins over TROVE_DB.
_explicit_db_path: Path | None = None


def get_trove_db_path() -> Path:
    """Return the current trove.db path.

    Resolution order:
      1. an explicit set_trove_db_path() override (tests, embedding apps);
      2. the TROVE_DB environment variable (deploy override, may contain
         ``~`` — expanded here);
      3. DEFAULT_DB_PATH (~/.hermes/trove.db).

    Read per call so a TROVE_DB set after import is honored.
    """
    if _explicit_db_path is not None:
        return _explicit_db_path
    raw = os.environ.get("TROVE_DB")
    if raw is not None and raw.strip():
        return Path(os.path.expanduser(raw.strip()))
    return DEFAULT_DB_PATH


def set_trove_db_path(path: Path) -> None:
    """Override the trove.db path (for testing / programmatic embeds).

    An explicit override wins over TROVE_DB until the process restarts.

    Args:
        path: New path to use for trove.db.
    """
    global _explicit_db_path
    _explicit_db_path = Path(path)


def connect(db_path, *, wal=True, busy_timeout_ms=5000):
    """Open a sqlite3 connection with WAL mode and busy timeout.

    Args:
        db_path: Path to the SQLite database file.
        wal: Enable WAL journal mode (default True).
        busy_timeout_ms: Busy timeout in milliseconds (default 5000).

    Returns:
        sqlite3.Connection configured for Trove's write pattern.
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL") if wal else None
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


@contextmanager
def write_transaction(conn, max_retries=3, retry_delay_ms=50):
    """Execute a write transaction with BEGIN IMMEDIATE and retry on lock.

    Issues BEGIN IMMEDIATE to grab the write lock early. Commits on clean
    exit, rolls back on exception. Retries on sqlite3.OperationalError
    "database is locked" with bounded backoff.

    Args:
        conn: An open sqlite3.Connection.
        max_retries: Maximum number of retry attempts (default 3).
        retry_delay_ms: Initial delay between retries in ms (default 50).

    Yields:
        The connection for the caller to execute statements.

    Raises:
        sqlite3.OperationalError: If max retries exhausted on "database is locked".
    """
    for attempt in range(max_retries + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return  # Success — exit without retry
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries:
                delay = retry_delay_ms * (2 ** attempt) / 1000  # Exponential backoff
                time.sleep(delay)
                continue
            raise
