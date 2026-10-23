"""scripts/e2e.sh reports every e2e test by node id and outcome, one line each.

pyproject's addopts carries -q, so a plain -v in the script nets out to the default dots and
a run's output names no test. Whoever reads the output (a person, CI, a verifier matching
`<name> ... PASSED`) then cannot tell which test passed without re-running each one alone,
and each lone run brings the whole kind environment up again.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e.sh"


def script_pytest_args() -> list[str]:
    """The arguments scripts/e2e.sh passes to `uv run pytest`."""
    lines = [ln for ln in SCRIPT.read_text().splitlines() if "uv run pytest" in ln]
    assert len(lines) == 1, lines
    argv = shlex.split(lines[0].split("uv run pytest", 1)[1])
    assert "tests/e2e" in argv, argv
    return argv


def test_script_run_names_each_passing_test(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe_e2e.py"
    probe.write_text(
        "import pytest\n\npytestmark = pytest.mark.e2e\n\n\n"
        "def test_probe_one():\n    pass\n\n\ndef test_probe_two():\n    pass\n"
    )
    argv = [str(probe) if a == "tests/e2e" else a for a in script_pytest_args()]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"), *argv],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for name in ("test_probe_one", "test_probe_two"):
        lines = [ln for ln in proc.stdout.splitlines() if name in ln and "PASSED" in ln]
        assert lines, (name, proc.stdout)
