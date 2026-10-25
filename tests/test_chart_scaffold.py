import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from helm_render import CHART, LINT_VALUES, lint, render, render_error

ROOT = CHART.parent.parent
LINT = yaml.safe_load(LINT_VALUES.read_text())


@pytest.fixture
def probe_chart(tmp_path: Path) -> Path:
    """A copy of the chart with one extra template that exercises the helpers."""
    dst = tmp_path / "agents-orchestrator"
    shutil.copytree(CHART, dst)
    (dst / "templates" / "probe.yaml").write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        '  name: {{ include "agents-orchestrator.fullname" . }}\n'
        "  labels:\n"
        '    {{- include "agents-orchestrator.labels" . | nindent 4 }}\n'
        "data:\n"
        "  selector: |\n"
        '    {{- include "agents-orchestrator.selectorLabels" . | nindent 4 }}\n'
        "  component: |\n"
        '    {{- include "agents-orchestrator.componentLabels" '
        '(dict "context" . "component" "api") | nindent 4 }}\n'
        '  sa: {{ include "agents-orchestrator.serviceAccountName" . }}\n'
    )
    return dst


def test_chart_metadata():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["apiVersion"] == "v2"
    assert chart["name"] == "agents-orchestrator"
    assert chart["type"] == "application"
    assert chart["version"] == "0.1.0"
    assert chart["appVersion"] == "dev"
    assert chart["description"]


def test_standard_helpers_and_labels(probe_chart: Path):
    (cm,) = render(LINT, chart=probe_chart, release="orch", show_only=["templates/probe.yaml"])
    labels = cm["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "agents-orchestrator"
    assert labels["app.kubernetes.io/instance"] == "orch"
    assert labels["app.kubernetes.io/version"] == "dev"
    assert labels["app.kubernetes.io/managed-by"] == "Helm"
    assert labels["helm.sh/chart"] == "agents-orchestrator-0.1.0"
    assert cm["metadata"]["name"] == "orch-agents-orchestrator"
    selector = yaml.safe_load(cm["data"]["selector"])
    assert selector == {
        "app.kubernetes.io/name": "agents-orchestrator",
        "app.kubernetes.io/instance": "orch",
    }
    component = yaml.safe_load(cm["data"]["component"])
    assert component == {**selector, "app.kubernetes.io/component": "api"}
    assert cm["data"]["sa"] == "orch-agents-orchestrator"


def test_helm_lint_passes():
    proc = lint()
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_hpa_dropped():
    tracked = subprocess.run(
        ["git", "ls-files", "chart"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    assert "hpa.yaml" not in tracked
    assert not (CHART / "templates" / "hpa.yaml").exists()
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert "autoscaling" not in values
    assert "livenessProbe" not in values and "readinessProbe" not in values
    assert values["image"]["repository"] == "harbor.szymonrichert.pl/containers/agents-orchestrator"
    assert values["podSecurityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "fsGroup": 1000,
    }


def test_render_helper_roundtrip(probe_chart: Path):
    docs = render(LINT, chart=probe_chart, show_only=["templates/probe.yaml"], namespace="ns1")
    assert [d["kind"] for d in docs] == ["ConfigMap"]
    # a bogus show-only path must fail and surface helm's text
    text = render_error(LINT, show_only=["templates/missing.yaml"], chart=probe_chart)
    assert "missing.yaml" in text
