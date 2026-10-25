"""The UI moved to agents-web: no frontend source, node stage or npm in this repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_frontend_untracked() -> None:
    out = subprocess.run(
        ["git", "ls-files", "frontend"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    assert out.strip() == ""


def test_dockerfile_has_no_frontend_stage() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM node" not in dockerfile
    assert "/app/static" not in dockerfile
    assert "--from=frontend" not in dockerfile


def test_ci_has_no_npm() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    assert "npm" not in ci
    assert "setup-node" not in ci
    assert "\n  frontend:" not in ci
