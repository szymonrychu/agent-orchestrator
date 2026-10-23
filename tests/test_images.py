"""Image CI and pin consistency (D16, D17, D22)."""

import re
import subprocess
import tomllib
from pathlib import Path

import yaml

from agent_orchestrator.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "harbor.szymonrichert.pl/containers/agent-orchestrator"


def _ci() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text())


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_ci_pushes_orchestrator_image_on_main_only():
    text = (ROOT / ".github/workflows/ci.yaml").read_text()
    job = _ci()["jobs"]["images"]
    steps = job["steps"]
    runs = [s.get("run", "") for s in steps]
    assert any("docker build" in r for r in runs)
    assert any("docker run --rm" in r and "--help" in r for r in runs)
    pushing = [s for s in steps if "regctl image import" in s.get("run", "")]
    assert len(pushing) == 1
    push = pushing[0]
    assert "github.ref == 'refs/heads/main'" in push["if"]
    assert "github.event_name == 'push'" in push["if"]
    assert job["env"]["IMAGE"] == IMAGE
    assert '"$IMAGE:$SHORTSHA"' in push["run"]
    assert any("${GITHUB_SHA::7}" in r and "SHORTSHA" in r for r in runs)
    logins = [s for s in steps if "registry login" in s.get("run", "")]
    assert len(logins) == 1
    assert "github.ref == 'refs/heads/main'" in logins[0]["if"]
    assert "HARBOR_USERNAME" in str(logins[0]) and "HARBOR_PASSWORD" in str(logins[0])
    # exactly one image reference is ever pushed
    assert set(re.findall(r"harbor\.szymonrichert\.pl/[\w./-]+", text)) == {
        IMAGE,
        "harbor.szymonrichert.pl/charts",
    }
    assert "pull_request" in _ci()[True]


def test_no_derived_agent_image():
    files = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert not [f for f in files if f.startswith("agent-image/")]
    ci = (ROOT / ".github/workflows/ci.yaml").read_text()
    assert "agent-image" not in ci
    assert "claude-code-wrapper" not in ci


def test_ccw_dev_dependency_matches_agent_image_sha():
    dev = _pyproject()["dependency-groups"]["dev"]
    pin = next(d for d in dev if d.startswith("claude-code-wrapper"))
    sha = re.search(r"claude-code-wrapper@([0-9a-f]{40})$", pin).group(1)
    readme = (ROOT / "README.md").read_text()
    images = readme.split("## Images", 1)[1].split("\n## ", 1)[0]
    ref = r"harbor\.szymonrichert\.pl/containers/claude-code-wrapper:([0-9a-f]{7})\b"
    m = re.search(ref, images)
    assert m, "README ## Images must name the agent image"
    assert m.group(1) == sha[:7]
    lock = (ROOT / "uv.lock").read_text()
    assert f"claude-code-wrapper?rev={sha}#{sha}" in lock


def test_pyproject_any_mcp_pin_matches_settings_ref():
    deps = _pyproject()["project"]["dependencies"]
    pin = next(d for d in deps if d.startswith("any-mcp"))
    tag = re.search(r"any-mcp@(v[\w.]+)$", pin).group(1)
    assert tag == Settings.model_fields["any_mcp_ref"].default
    lock = (ROOT / "uv.lock").read_text()
    assert f"any-mcp?rev={tag}#" in lock
