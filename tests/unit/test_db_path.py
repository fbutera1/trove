"""TROVE_DB path-resolution tests (Phase 4, config consolidation).

T51 — get_trove_db_path() honors TROVE_DB (deploy override, ``~`` expanded,
      whitespace treated as unset, default when unset).
T52 — init_db(None) creates the schema at the TROVE_DB path (the
      capture-path proof: schema init follows the env var, not the
      hardcoded default).
T53 — an explicit set_trove_db_path() override wins over TROVE_DB.
T54 — TROVE_DB set after import is honored (per-call env read).
"""

from pathlib import Path

import pytest

from trove import db as trove_db
from trove.db import DEFAULT_DB_PATH, get_trove_db_path, set_trove_db_path
from trove.schema import init_db


# ── T51: get_trove_db_path() honors TROVE_DB ─────────────────────────


def test_T51_trove_db_env_var_honored(tmp_path, monkeypatch):
    """T51: TROVE_DB set → get_trove_db_path() returns that path."""
    target = tmp_path / "custom.db"
    monkeypatch.setenv("TROVE_DB", str(target))
    assert get_trove_db_path() == target


def test_T51_trove_db_tilde_expanded(monkeypatch):
    """T51 (tilde): TROVE_DB may contain ``~``; it is expanded."""
    monkeypatch.setenv("TROVE_DB", "~/trove-override.db")
    assert get_trove_db_path() == Path.home() / "trove-override.db"


def test_T51_blank_trove_db_treated_as_unset(monkeypatch):
    """T51 (blank): whitespace-only TROVE_DB is treated as unset."""
    monkeypatch.setenv("TROVE_DB", "   ")
    assert get_trove_db_path() == DEFAULT_DB_PATH


def test_T51_default_when_unset(monkeypatch):
    """T51 (default): TROVE_DB unset → DEFAULT_DB_PATH."""
    monkeypatch.delenv("TROVE_DB", raising=False)
    assert get_trove_db_path() == DEFAULT_DB_PATH


# ── T52: init_db(None) follows TROVE_DB ──────────────────────────────


def test_T52_init_db_none_uses_trove_db_path(tmp_path, monkeypatch):
    """T52: init_db(None) with TROVE_DB set creates the schema at the
    TROVE_DB path — not at the default path (the capture lazy-init path
    is schema.init_db(db_path) with the resolver's result, and a direct
    init_db(None) call must land in the same place)."""
    target = tmp_path / "nested" / "trove.db"
    monkeypatch.setenv("TROVE_DB", str(target))
    conn = init_db(None)
    try:
        assert target.exists()
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version == 4
    finally:
        conn.close()


# ── T53: explicit override wins over TROVE_DB ────────────────────────


def test_T53_explicit_override_wins_over_env(tmp_path, monkeypatch):
    """T53: set_trove_db_path() wins over TROVE_DB until the process
    restarts (programmatic/test overrides are the strongest signal)."""
    env_target = tmp_path / "from-env.db"
    explicit_target = tmp_path / "explicit.db"
    monkeypatch.setenv("TROVE_DB", str(env_target))
    # Restore the module global after the test.
    monkeypatch.setattr(trove_db, "_explicit_db_path", None, raising=False)

    set_trove_db_path(explicit_target)
    assert get_trove_db_path() == explicit_target


# ── T54: per-call env read (set after import) ────────────────────────


def test_T54_env_set_after_import_honored(tmp_path, monkeypatch):
    """T54: TROVE_DB read per call — setting it after module import
    (e.g. dotenv loading at deploy time) is honored."""
    monkeypatch.delenv("TROVE_DB", raising=False)
    assert get_trove_db_path() == DEFAULT_DB_PATH

    target = tmp_path / "late.db"
    monkeypatch.setenv("TROVE_DB", str(target))
    assert get_trove_db_path() == target
