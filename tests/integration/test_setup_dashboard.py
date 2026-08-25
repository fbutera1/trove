"""deploy/setup.sh dashboard section tests (T59, T60).

The dashboard section (post post-consolidation Phase 3, C2a/C2b):
generates the trove-only env file (``<profile>/.env.trove``, TROVE_*
keys only) and installs a rendered systemd user unit from
``deploy/systemd/trove-dashboard.service``.

These run ``deploy/setup.sh`` in a fake HOME so no live profile,
systemd, or repo venv is touched. The script's link/copy sections run
against a real (tiny) deploy tree and are exercised as collateral —
they must not fail; the assertions are on the dashboard section.

T59 — TROVE-only extraction: SIGNAL_* keys are NOT copied; TROVE_*
      keys with values are; empty-valued TROVE_ keys are dropped; the
      unit is rendered with all three placeholders substituted, binds
      loopback (no 0.0.0.0), and references the generated env file.
T60 — idempotence: a second run reports "already up to date" for both
      the env file and the unit; a TROVE_PEOPLE change in the profile
      .env is picked up on re-run (snapshot refresh).
"""

import os
import re
import shutil
import subprocess
import textwrap

import pytest

REPO_SOURCE = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


@pytest.fixture(scope="module")
def repo_copy(tmp_path_factory):
    """A scratch copy of the repo (minus .git/.venv) so setup.sh's
    repo-root AGENTS.md link and the rendered unit's WorkingDirectory
    never touch the real checkout."""
    dest = tmp_path_factory.mktemp("trove-repo")
    for item in os.listdir(REPO_SOURCE):
        if item in (".git", ".venv"):
            continue
        src = os.path.join(REPO_SOURCE, item)
        if os.path.isdir(src):
            shutil.copytree(src, str(dest / item))
        else:
            shutil.copy2(src, str(dest / item))
    return str(dest)


def _run_setup(repo: str, fake_home: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = fake_home
    env["HERMES_HOME"] = os.path.join(fake_home, ".hermes")
    # Keep the real profile/env out of the script's view:
    env.pop("TROVE_PEOPLE", None)
    env.pop("TROVE_DB", None)
    return subprocess.run(
        ["bash", os.path.join(repo, "deploy", "setup.sh"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _unit_path(fake_home: str) -> str:
    return os.path.join(fake_home, ".config", "systemd", "user", "trove-dashboard.service")


@pytest.fixture
def fake_host(tmp_path):
    """A fake HOME with a minimal Hermes home + two-profile layout."""
    fake_home = str(tmp_path / "home")
    profile = os.path.join(fake_home, ".hermes", "profiles", "trove-partners")
    _write(
        os.path.join(profile, ".env"),
        textwrap.dedent(
            """\
            SIGNAL_ACCOUNT=+10000000000
            TROVE_PEOPLE=+131****0100:Alex,00000000-0000-4000-8000-000000000001:Alex
            TROVE_DB=
            TROVE_PEOPLE_DUPCHECK=x
            """
        ),
    )
    return {
        "home": fake_home,
        "profile": profile,
        "env_trove": os.path.join(profile, ".env.trove"),
        "unit": os.path.join(fake_home, ".config", "systemd", "user", "trove-dashboard.service"),
    }


def test_T59_trove_only_env_and_rendered_unit(fake_host, repo_copy):
    """T59: the generated env file carries TROVE_* keys with values
    only (no SIGNAL_*, no empty-valued keys), and the rendered unit
    has all placeholders substituted, binds loopback, and references
    the generated env file."""
    result = _run_setup(repo_copy, fake_host["home"], "partners")
    assert result.returncode == 0, result.stdout + result.stderr

    env_content = open(fake_host["env_trove"], encoding="utf-8").read()
    env_lines = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_content.splitlines()
        if line.strip()
    }
    # TROVE keys with values copied:
    assert "TROVE_PEOPLE" in env_lines
    assert "00000000-0000-4000-8000-000000000001" in env_lines["TROVE_PEOPLE"]
    assert "TROVE_PEOPLE_DUPCHECK" in env_lines
    # Empty-valued TROVE key dropped:
    assert "TROVE_DB" not in env_lines
    # No SIGNAL_* exposure to the dashboard process:
    assert "SIGNAL_ACCOUNT" not in env_lines
    assert "SIGNAL" not in env_content

    # Rendered unit: placeholders gone, loopback bind, env file wired.
    unit = open(fake_host["unit"], encoding="utf-8").read()
    assert "__TROVE_BIN__" not in unit
    assert "__ENV_FILE__" not in unit
    assert "__REPO__" not in unit
    assert "0.0.0.0" not in unit  # D3: loopback only (no --host flag at all)
    assert re.search(
        r"^EnvironmentFile=" + re.escape(fake_host["env_trove"]) + r"$", unit, re.M
    )  # plain key — systemd does not support a dash-prefixed form
    assert "ssh -L 9120:127.0.0.1:9120" in unit

    # ExecStart must be `<bin> dashboard` — the substituted token IS the
    # `trove` console script, so a `trove` argument of its own would make
    # the service exit 0 and restart-loop (regression guard).
    assert re.search(r"^ExecStart=\S+ dashboard$", unit, re.M)


def test_T60_idempotent_and_snapshot_refresh(fake_host, repo_copy):
    """T60: a second run is a no-op ("already up to date"); after a
    TROVE_PEOPLE change in the profile .env, a re-run refreshes the
    snapshot."""
    result1 = _run_setup(repo_copy, fake_host["home"], "partners")
    assert result1.returncode == 0, result1.stdout + result1.stderr

    result2 = _run_setup(repo_copy, fake_host["home"], "partners")
    assert result2.returncode == 0, result2.stdout + result2.stderr
    assert "already up to date" in result2.stdout

    # Snapshot refresh: change TROVE_PEOPLE in the profile .env and re-run.
    profile_env = os.path.join(fake_host["profile"], ".env")
    content = open(profile_env, encoding="utf-8").read()
    content = content.replace(
        "+131****0100:Alex", "+131****0100:Avery"
    )
    _write(profile_env, content)

    result3 = _run_setup(repo_copy, fake_host["home"], "partners")
    assert result3.returncode == 0, result3.stdout + result3.stderr
    refreshed = open(fake_host["env_trove"], encoding="utf-8").read()
    assert "+131****0100:Avery" in refreshed
    assert "+131****0100:Alex" not in refreshed

    # Re-runs never leave stray backups when nothing changed (T60.1
    # idempotence check: the unit was untouched on run 2, so no new
    # backup file appeared):
    unit_dir = os.path.dirname(fake_host["unit"])
    backups = [f for f in os.listdir(unit_dir) if ".pre-trove-setup." in f]
    assert len(backups) == 0


def test_T60b_no_dashboard_flag_skips_section(fake_host, repo_copy):
    """T60b: --no-dashboard installs neither the env file nor the unit."""
    result = _run_setup(repo_copy, fake_host["home"], "partners", "--no-dashboard")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not os.path.exists(fake_host["env_trove"])
    assert not os.path.exists(fake_host["unit"])
    assert "Dashboard:" not in result.stdout


def test_T60c_profile_detection_live_shape(tmp_path, repo_copy):
    """T60c: live-host profile layout — two profile dirs (`trove-agent`,
    `trove-mechanic`), deploy type `partners` (no `partners` /
    `trove-partners` dir, two profiles so the sole-profile rule does
    not fire). Detection must land on the profile whose .env carries
    TROVE_PEOPLE (the gateway profile), not the type-matched name."""
    fake_home = str(tmp_path / "home2")
    agent = os.path.join(fake_home, ".hermes", "profiles", "trove-agent")
    mechanic = os.path.join(fake_home, ".hermes", "profiles", "trove-mechanic")
    _write(
        os.path.join(agent, ".env"),
        "SIGNAL_ACCOUNT=+10000000000\n"
        "TROVE_PEOPLE=+131****0100:Alex,00000000-0000-4000-8000-000000000001:Alex\n",
    )
    _write(os.path.join(mechanic, ".env"), "TAVILY_API_KEY=x\n")

    result = _run_setup(repo_copy, fake_home, "partners")
    assert result.returncode == 0, result.stdout + result.stderr

    env_trove = os.path.join(agent, ".env.trove")
    assert os.path.exists(env_trove), result.stdout
    assert os.path.exists(_unit_path(fake_home)), result.stdout
    # The env file went to the TROVE_PEOPLE profile, not the type name:
    assert not os.path.exists(os.path.join(mechanic, ".env.trove"))


def test_T60d_profile_flag_explicit(tmp_path, repo_copy):
    """T60d: --profile <name> wins over auto-detection; a nonexistent
    name warns and skips the dashboard section (rc still 0)."""
    fake_home = str(tmp_path / "home3")
    agent = os.path.join(fake_home, ".hermes", "profiles", "trove-agent")
    _write(
        os.path.join(agent, ".env"),
        "TROVE_PEOPLE=+131****0100:Alex\n",
    )

    # Explicit, existing profile:
    result = _run_setup(repo_copy, fake_home, "solo", "--profile", "trove-agent")
    assert result.returncode == 0, result.stdout + result.stderr
    assert os.path.exists(os.path.join(agent, ".env.trove"))

    # Explicit, missing profile: warn + skip, rc 0.
    result2 = _run_setup(repo_copy, fake_home, "solo", "--profile", "no-such-profile")
    assert result2.returncode == 0, result2.stdout + result2.stderr
    assert "does not exist" in result2.stdout
