"""Trove-only env file (`.env.trove`) loader tests.

T55 — parse_trove_env_file: KEY=VALUE, export prefix, comments,
      blank lines, quoted values, inline comments, last-occurrence
      wins, malformed lines skipped.
T56 — load_trove_env_file: missing file is a no-op (None); a present
      file loads into os.environ; an existing process env var is never
      overwritten; explicit path beats the search.
T57 — find_trove_env_file: walks up from the cwd (cwd first), returns
      None when no .env.trove is found.
T58 — CLI wiring: `trove dashboard` runs load_trove_env_file() before
      resolving the DB path (asserted on the CLI source contract; the
      loader itself is covered by T55–T57).
"""

import os
import textwrap

import pytest

from trove import cli, env


# ── T55: parser semantics ──────────────────────────────────────────────


def test_T55_parser_core_semantics():
    """T55: parser handles the dotenv subset the setup.sh generator
    can produce, plus the hand-edit shapes."""
    text = textwrap.dedent(
        """\
        # a comment line

        TROVE_PEOPLE=+131****0100:Alex,00000000-0000-4000-8000-000000000001:Alex
        export TROVE_DB=/tmp/x/trove.db
        TROVE_QUOTED_DOUBLE="double quoted # not a comment"
        TROVE_QUOTED_SINGLE='single # kept'
        TROVE_INLINE=foo bar # trailing comment stripped
        TROVE_EMPTY=
        not a key line
        =nokey
        TROVE_DUP=first
        TROVE_DUP=second
        """
    )
    values = env.parse_trove_env_file(text)
    assert values["TROVE_PEOPLE"] == (
        "+131****0100:Alex,00000000-0000-4000-8000-000000000001:Alex"
    )
    assert values["TROVE_DB"] == "/tmp/x/trove.db"  # export prefix accepted
    assert values["TROVE_QUOTED_DOUBLE"] == "double quoted # not a comment"
    assert values["TROVE_QUOTED_SINGLE"] == "single # kept"
    assert values["TROVE_INLINE"] == "foo bar"  # inline comment stripped
    assert values["TROVE_EMPTY"] == ""
    assert values["TROVE_DUP"] == "second"  # last occurrence wins
    # Malformed lines are skipped, not fatal:
    assert "not" not in values
    assert len(values) == 7


# ── T56: load semantics (no overwrite, missing file no-op) ─────────────


def test_T56_missing_file_is_noop(tmp_path, monkeypatch):
    """T56: an explicit missing path returns None and touches nothing."""
    monkeypatch.setenv("TROVE_PEOPLE", "preexisting")
    result = env.load_trove_env_file(path=tmp_path / "does-not-exist")
    assert result is None
    assert os.environ["TROVE_PEOPLE"] == "preexisting"


def test_T56_load_sets_unset_vars_only(tmp_path, monkeypatch):
    """T56: keys from the file land in os.environ, but a variable
    already present in the process environment is never overwritten
    (explicit env wins over the file)."""
    env_file = tmp_path / ".env.trove"
    env_file.write_text(
        "TROVE_PEOPLE=from-file\n"
        "TROVE_DB=/tmp/from-file.db\n"
        "TROVE_KEEP=keep-me\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TROVE_KEEP", "process-wins")
    monkeypatch.delenv("TROVE_PEOPLE", raising=False)
    monkeypatch.delenv("TROVE_DB", raising=False)

    result = env.load_trove_env_file(path=env_file)
    assert result == env_file
    assert os.environ["TROVE_PEOPLE"] == "from-file"
    assert os.environ["TROVE_DB"] == "/tmp/from-file.db"
    assert os.environ["TROVE_KEEP"] == "process-wins"  # NOT overwritten


# ── T57: cwd-upward search ─────────────────────────────────────────────


def test_T57_find_walks_up_from_cwd(tmp_path, monkeypatch):
    """T57: find_trove_env_file() returns the nearest .env.trove
    walking up from the cwd (cwd first, then parents)."""
    leaf = tmp_path / "a" / "b"
    leaf.mkdir(parents=True)
    (tmp_path / ".env.trove").write_text("TROVE_DB=root\n", encoding="utf-8")
    (leaf / ".env.trove").write_text("TROVE_DB=leaf\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path / "a")  # not the cwd itself; parent has one
    found = env.find_trove_env_file()
    assert found == tmp_path / ".env.trove"

    monkeypatch.chdir(leaf)  # cwd itself has one — closest wins
    assert env.find_trove_env_file() == leaf / ".env.trove"


def test_T57_find_returns_none_when_absent(tmp_path, monkeypatch):
    """T57: no .env.trove anywhere above the cwd → None."""
    empty = tmp_path / "nowhere"
    empty.mkdir()
    monkeypatch.chdir(empty)
    found = env.find_trove_env_file()
    # `empty` and tmp_path have no .env.trove; if one is found at all,
    # it must be ABOVE tmp_path (outside the test sandbox) — which
    # would be a pre-existing host file, not one this test created.
    assert found is None or (tmp_path not in found.parents)


# ── T58: CLI wiring contract ───────────────────────────────────────────


def test_T58_cli_dashboard_loads_trove_env(monkeypatch, tmp_path):
    """T58: `trove dashboard` invokes load_trove_env_file() BEFORE
    resolving the DB path, and a missing file is a no-op (no crash).

    The server is stubbed: the dashboard subcommand's job under test is
    the env-loading order, not starting uvicorn.
    """
    import argparse

    import trove.dashboard.server as server_mod

    calls = []

    def fake_load(path=None):
        calls.append(("load", path))
        # Simulate the production unit's EnvironmentFile content:
        os.environ["TROVE_DB"] = str(tmp_path / "from-env-file.db")
        return None  # mimic "no file found" for the search path

    def fake_run(**kwargs):
        calls.append(("run", kwargs))

    monkeypatch.setattr(env, "load_trove_env_file", fake_load)
    monkeypatch.setattr(server_mod, "run", fake_run)
    monkeypatch.delenv("TROVE_DB", raising=False)
    monkeypatch.setattr(cli.sys, "argv", ["trove", "dashboard"])

    # _run_dashboard takes a parsed-args namespace:
    args = argparse.Namespace(host="127.0.0.1", port=9120, db_path=None)
    assert cli._run_dashboard(args) == 0

    assert calls[0] == ("load", None)  # loaded before anything else
    assert calls[1][0] == "run"
    # The DB path resolved AFTER the env load used the file's TROVE_DB:
    assert calls[1][1]["db_path"] == tmp_path / "from-env-file.db"
