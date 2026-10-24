"""Component Deployments, Service, ServiceAccounts and helm test of the chart (D28, D1, D9, D20)."""

from __future__ import annotations

from typing import Any

from helm_render import render

FULL = "agent-orchestrator"
COMPONENTS = ["api", "dispatcher", "ingester", "reconciler"]
BASE: dict[str, Any] = {
    "agentImage": "registry.example/wrapper:1",
    "masterKey": {"value": "mk"},
    "claude": {"token": "tok"},
    "postgres": {"url": "postgresql://u:p@db:5432/app"},
}


def _deployments(values: dict[str, Any] | None = None) -> dict[str, dict]:
    docs = render({**BASE, **(values or {})})
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}


def _container(dep: dict) -> dict:
    return dep["spec"]["template"]["spec"]["containers"][0]


def test_four_deployments_with_migrate_init() -> None:
    deps = _deployments()
    assert sorted(deps) == sorted(f"{FULL}-{c}" for c in COMPONENTS)
    for c in COMPONENTS:
        dep = deps[f"{FULL}-{c}"]
        spec = dep["spec"]["template"]["spec"]
        assert dep["metadata"]["labels"]["app.kubernetes.io/component"] == c
        assert dep["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == c
        (init,) = spec["initContainers"]
        assert init["name"] == "migrate"
        assert init["args"] == ["migrate"] or init["command"][-1] == "migrate"
        assert init["image"] == _container(dep)["image"]
        assert _container(dep)["env"][: len(init["env"])] == init["env"]
        assert init["envFrom"] == _container(dep)["envFrom"]
        names = [e["name"] for e in init["env"]]
        assert "ORCH_DATABASE_URL" in names and "ORCH_MASTER_KEY" in names
        assert spec["terminationGracePeriodSeconds"] == 30


def test_component_commands_and_values() -> None:
    values = {
        "components": {
            "api": {"replicas": 2, "resources": {"limits": {"memory": "1Gi"}}},
            "dispatcher": {"extraArgs": ["--verbose"]},
            "reconciler": {"replicas": 3},
        }
    }
    deps = _deployments(values)
    for c in COMPONENTS:
        cont = _container(deps[f"{FULL}-{c}"])
        assert cont["command"] == ["orch"]
        assert cont["args"][0] == c
    assert deps[f"{FULL}-api"]["spec"]["replicas"] == 2
    assert deps[f"{FULL}-reconciler"]["spec"]["replicas"] == 3
    assert deps[f"{FULL}-ingester"]["spec"]["replicas"] == 1
    assert _container(deps[f"{FULL}-api"])["resources"] == {"limits": {"memory": "1Gi"}}
    assert _container(deps[f"{FULL}-dispatcher"])["args"] == ["dispatcher", "--verbose"]


def test_only_reconciler_gets_kube_token() -> None:
    deps = _deployments()
    for c in COMPONENTS:
        spec = deps[f"{FULL}-{c}"]["spec"]["template"]["spec"]
        if c == "reconciler":
            assert spec["serviceAccountName"] == f"{FULL}-reconciler"
            assert spec.get("automountServiceAccountToken") is not False
        else:
            assert spec["automountServiceAccountToken"] is False
            assert spec["serviceAccountName"] != f"{FULL}-reconciler"
    sas = {
        d["metadata"]["name"]: d
        for d in render({**BASE}, show_only=["templates/serviceaccount.yaml"])
    }
    assert set(sas) == {FULL, f"{FULL}-reconciler"}
    assert sas[FULL]["automountServiceAccountToken"] is False
    off = render({**BASE, "serviceAccount": {"create": False}}, show_only=None)
    assert not [d for d in off if d["kind"] == "ServiceAccount"]


def test_api_service_and_probes() -> None:
    docs = render(BASE)
    (svc,) = [d for d in docs if d["kind"] == "Service"]
    assert svc["metadata"]["name"] == FULL
    assert svc["spec"]["selector"]["app.kubernetes.io/component"] == "api"
    (port,) = svc["spec"]["ports"]
    assert port["port"] == 80 and port["targetPort"] == "http"
    deps = _deployments()
    api = _container(deps[f"{FULL}-api"])
    assert api["ports"] == [{"name": "http", "containerPort": 8000, "protocol": "TCP"}]
    assert api["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": "http"}
    assert api["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": "http"}
    assert {"name": "ORCH_HTTP_PORT", "value": "8000"} in api["env"]
    for c in ("dispatcher", "ingester", "reconciler"):
        cont = _container(deps[f"{FULL}-{c}"])
        assert "livenessProbe" not in cont and "readinessProbe" not in cont
        assert "ports" not in cont
    custom = _deployments(
        {"components": {"ingester": {"livenessProbe": {"exec": {"command": ["true"]}}}}}
    )
    assert _container(custom[f"{FULL}-ingester"])["livenessProbe"] == {
        "exec": {"command": ["true"]}
    }


def test_image_tag_defaults_to_app_version() -> None:
    repo = "harbor.szymonrichert.pl/containers/agent-orchestrator"
    for dep in _deployments().values():
        assert _container(dep)["image"] == f"{repo}:dev"
    for dep in _deployments({"image": {"tag": "9.9.9"}}).values():
        assert _container(dep)["image"] == f"{repo}:9.9.9"
        assert dep["spec"]["template"]["spec"]["initContainers"][0]["image"] == f"{repo}:9.9.9"


def test_helm_test_targets_healthz() -> None:
    (pod,) = render(BASE, show_only=["templates/tests/test-connection.yaml"])
    assert pod["metadata"]["annotations"]["helm.sh/hook"] == "test"
    args = pod["spec"]["containers"][0]["args"]
    assert args == [f"{FULL}:80/healthz"]


def test_settings_checksum_rolls_components() -> None:
    before = _deployments()
    after = _deployments({"env": {"ORCH_LOG_LEVEL": "debug"}})
    for c in COMPONENTS:
        name = f"{FULL}-{c}"
        a = before[name]["spec"]["template"]["metadata"]["annotations"]["checksum/settings"]
        b = after[name]["spec"]["template"]["metadata"]["annotations"]["checksum/settings"]
        assert a and b and a != b


def test_component_pod_annotations_reach_only_that_component() -> None:
    # A sidecar-injecting webhook (oauth2-proxy admission) rejects port-less pods, so its
    # annotations must land on the api pod alone; global podAnnotations still reach all.
    deps = _deployments(
        {
            "podAnnotations": {"shared": "yes"},
            "components": {"api": {"podAnnotations": {"only-api": "true", "shared": "api"}}},
        }
    )
    for c in COMPONENTS:
        ann = deps[f"{FULL}-{c}"]["spec"]["template"]["metadata"]["annotations"]
        if c == "api":
            assert ann["only-api"] == "true"
            assert ann["shared"] == "api"
        else:
            assert "only-api" not in ann
            assert ann["shared"] == "yes"
        assert ann["checksum/settings"]
