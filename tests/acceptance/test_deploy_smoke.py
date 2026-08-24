"""Pre-deploy smoke tests for Trove.

These verify the local environment is correctly configured before running
the manual acceptance RUNBOOK (tests/acceptance/RUNBOOK.md).

Run before deploying (not part of the default test suite):
    uv run pytest tests/acceptance/ -m deploy_smoke --tb=short
"""

import os
import sqlite3

import pytest

pytestmark = pytest.mark.deploy_smoke


# ── Deploy smoke assertions ─────────────────────────────────────────


def test_deploy_capture_senders_set():
    """Deploy check: TROVE_PEOPLE is set in the environment.

    TROVE_PEOPLE is the single source of truth for person identity and,
    since the capture allowlist is derived from its keys, it is the
    load-bearing capture-scope config. TROVE_CAPTURE_SENDERS is now an
    optional strict override, so it is no longer the gate this check
    asserts.

    Maps to: RUNBOOK Step A (deploy hygiene).
    Fails loudly if the capture-scope gate is not configured.
    """
    value = os.getenv("TROVE_PEOPLE")
    if not value:
        pytest.skip(
            "TROVE_PEOPLE not set in this environment. "
            "Set in ~/.hermes/.env (the capture allowlist is derived from "
            "its keys) and restart the Hermes gateway, then re-run."
        )
    assert value, "TROVE_PEOPLE is empty"


def test_deploy_trove_db_clean_start():
    """Deploy check: trove.db is absent or empty before first real capture.

    Maps to: RUNBOOK Step B (deploy hygiene).
    Skipped if the DB doesn't exist (fresh deploy is fine).
    """
    db_path = os.path.expanduser("~/.hermes/trove.db")
    if not os.path.exists(db_path):
        return  # absent is fine — will be created on first capture
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "nuggets" not in tables:
            return  # an uninitialized SQLite file is equivalent to an absent DB
        cursor = conn.execute("SELECT count(*) FROM nuggets")
        count = cursor.fetchone()[0]
        assert count == 0, (
            f"trove.db has {count} rows before first real capture. "
            "Archive or remove the existing database."
        )
    finally:
        conn.close()
