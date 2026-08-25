"""T62 — Dashboard frontend smoke test (node DOM-stub harness).

Runs `tests/frontend/dashboard_smoke.cjs`, which extracts the inline
<script> from `trove/dashboard/static/index.html`, executes it against a
minimal DOM stub with a canned fetch, and asserts:

  - initial load hits /api/nuggets (not the tasks endpoint),
  - Nuggets cards render the author label (author_label, never a raw ID),
  - switching to the Tasks view hits /api/nuggets/tasks,
  - task cards render due + assignee chips (overdue styling for past due),
  - horizon/assignee filters pass query params,
  - the drawer renders Assignee / Due / Author fields for tasks.

Skipped when node is not installed (the harness is a dev-box convenience,
not a CI requirement).
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "tests" / "frontend" / "dashboard_smoke.cjs"


def test_T62_dashboard_frontend_smoke():
    """T62: the dashboard frontend script executes end-to-end against the
    stub DOM and drives the expected fetch sequence for both views."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    assert node is not None  # narrows for static checkers

    result = subprocess.run(
        [node, str(SMOKE_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "frontend smoke failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "SMOKE OK" in result.stdout
