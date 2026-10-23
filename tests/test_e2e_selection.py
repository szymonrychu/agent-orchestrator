"""The kind e2e tests run when a command line names them, and only then (tests/e2e/conftest.py)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = "tests/e2e/test_e2e_kind.py::test_round_robin_by_free_since"


def collect(*args: str) -> tuple[int, str]:
    """pytest --collect-only; pyproject's addopts -q makes it list node ids."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout


def test_named_e2e_node_id_is_collected() -> None:
    rc, out = collect(NODE)
    assert rc == 0, out
    assert NODE in out


def test_named_e2e_file_collects_every_e2e_test() -> None:
    rc, out = collect("tests/e2e/test_e2e_kind.py")
    assert rc == 0, out
    assert "test_e2e_kind.py::test_shared_namespace_leaves_orchestrator_objects" in out


def test_default_run_still_excludes_e2e() -> None:
    rc, out = collect("tests/e2e", "tests/test_settings.py")
    assert rc == 0, out
    assert "test_e2e_kind.py" in out  # named, so kept
    rc, out = collect("tests/test_settings.py")
    assert rc == 0, out
    assert "test_e2e_kind.py" not in out


def test_explicit_markexpr_wins_over_naming() -> None:
    rc, out = collect("-m", "not e2e", NODE)
    assert rc == 5, out
    assert NODE not in out
