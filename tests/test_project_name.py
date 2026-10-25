"""The Python project is agents-orchestrator (design D1, D10)."""

from __future__ import annotations

import importlib
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def test_old_package_retired() -> None:
    out = _git("ls-files", "src/agent_orchestrator")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""
    assert _git("ls-files", "src/agents_orchestrator/__init__.py").stdout.strip()


def test_package_named_agents_orchestrator() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "agents-orchestrator"
    assert project["project"]["scripts"] == {"orch": "agents_orchestrator.cli:main"}
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/agents_orchestrator"]

    labels = importlib.import_module("agents_orchestrator.k8s.labels")
    settings = importlib.import_module("agents_orchestrator.settings")
    assert labels.MANAGED_BY == "agents-orchestrator"
    default = settings.Settings.model_fields["claude_secret_name"].default
    assert default == "agents-orchestrator-claude"


def test_old_name_gone_from_src_and_build_files() -> None:
    paths = ("src/", "pyproject.toml", "Dockerfile", "openapi.json")
    out = _git("grep", "-n", "-E", "agent[-_]orchestrator", "--", *paths)
    # git grep exits 1 when nothing matches, 0 with matches, >1 on error
    assert out.returncode == 1, out.stdout or out.stderr
