"""Shared helm render helper for the chart tests. A missing helm binary fails, never skips."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parent.parent / "chart" / "agent-orchestrator"
LINT_VALUES = CHART / "ci" / "lint-values.yaml"


def _helm() -> str:
    path = shutil.which("helm")
    if path is None:
        raise AssertionError("helm binary not found on PATH; the chart tests require it")
    return path


def _template_cmd(
    values: dict[str, Any] | None,
    values_file: Path,
    show_only: list[str] | None,
    namespace: str,
    release: str,
    chart: Path,
) -> list[str]:
    values_file.write_text(yaml.safe_dump(values or {}))
    cmd = [
        _helm(),
        "template",
        release,
        str(chart),
        "--namespace",
        namespace,
        "-f",
        str(values_file),
    ]
    for path in show_only or []:
        cmd += ["--show-only", path]
    return cmd


def render(
    values: dict[str, Any] | None = None,
    *,
    show_only: list[str] | None = None,
    namespace: str = "agents",
    release: str = "agent-orchestrator",
    chart: Path = CHART,
) -> list[dict]:
    """helm template with a temp values file; returns the parsed (non-empty) documents."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = _template_cmd(values, Path(tmp) / "values.yaml", show_only, namespace, release, chart)
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"helm template failed:\n{proc.stderr}")
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def render_error(
    values: dict[str, Any] | None = None,
    *,
    show_only: list[str] | None = None,
    namespace: str = "agents",
    release: str = "agent-orchestrator",
    chart: Path = CHART,
) -> str:
    """stderr of a helm template that must fail."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = _template_cmd(values, Path(tmp) / "values.yaml", show_only, namespace, release, chart)
        proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode != 0, "helm template succeeded but was expected to fail"
    return proc.stderr


def lint(values_file: Path | str = LINT_VALUES, chart: Path = CHART) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_helm(), "lint", str(chart), "-f", str(values_file)], capture_output=True, text=True
    )
