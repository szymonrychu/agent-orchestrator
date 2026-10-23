"""Reconciler Role, RoleBinding and the optional ingress of the chart (D9, D26, D28)."""

from __future__ import annotations

import re
from typing import Any

import yaml
from helm_render import CHART, render

FULL = "agent-orchestrator"
BASE: dict[str, Any] = {
    "agentImage": "registry.example/wrapper:1",
    "masterKey": {"value": "mk"},
    "claude": {"token": "tok"},
    "postgres": {"url": "postgresql://u:p@db:5432/app"},
}
CRUD = ["create", "delete", "get", "list", "patch", "watch"]


def _docs(values: dict[str, Any] | None = None) -> list[dict]:
    return render({**BASE, **(values or {})})


def _by_kind(kind: str, values: dict[str, Any] | None = None) -> list[dict]:
    return [d for d in _docs(values) if d["kind"] == kind]


def _rules(role: dict) -> set[tuple[str, str, tuple[str, ...]]]:
    out = set()
    for rule in role["rules"]:
        for group in rule["apiGroups"]:
            for res in rule["resources"]:
                out.add((group, res, tuple(sorted(rule["verbs"]))))
    return out


def test_role_has_exactly_d9_rules() -> None:
    (role,) = _by_kind("Role")
    assert role["metadata"]["name"] == f"{FULL}-reconciler"
    assert role["metadata"]["namespace"] == "agents"
    expected = {
        ("apps", "statefulsets", tuple(CRUD)),
        ("", "services", tuple(CRUD)),
        ("", "configmaps", tuple(CRUD)),
        ("", "persistentvolumeclaims", tuple(CRUD)),
        ("", "secrets", ("create", "delete", "patch")),
        ("", "pods", ("delete", "get", "list", "watch")),
        ("batch", "jobs", ("create", "delete", "get", "list", "watch")),
        ("", "pods/log", ("get",)),
    }
    assert _rules(role) == expected
    assert len(role["rules"]) == len(expected)


def test_secrets_write_only_and_no_exec() -> None:
    (role,) = _by_kind("Role")
    for rule in role["rules"]:
        if "secrets" in rule["resources"]:
            assert sorted(rule["verbs"]) == ["create", "delete", "patch"]
        assert "pods/exec" not in rule["resources"]
        assert "*" not in rule["resources"]
        assert "*" not in rule["verbs"]
        assert "*" not in rule["apiGroups"]


def test_rolebinding_binds_reconciler_only() -> None:
    (rb,) = _by_kind("RoleBinding")
    assert rb["metadata"]["namespace"] == "agents"
    assert rb["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": f"{FULL}-reconciler",
    }
    assert rb["subjects"] == [
        {"kind": "ServiceAccount", "name": f"{FULL}-reconciler", "namespace": "agents"}
    ]
    kinds = {d["kind"] for d in _docs()}
    assert not kinds & {"ClusterRole", "ClusterRoleBinding"}


def test_rbac_create_false() -> None:
    kinds = {d["kind"] for d in _docs({"rbac": {"create": False}})}
    assert not kinds & {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}


def test_ingress_optional_from_values() -> None:
    assert _by_kind("Ingress") == []
    values = yaml.safe_load(open(CHART / "values.yaml"))
    assert values["ingress"]["enabled"] is False
    assert values["ingress"]["className"] == ""
    assert values["ingress"]["annotations"] == {}
    (ing,) = _by_kind(
        "Ingress",
        {
            "ingress": {
                "enabled": True,
                "className": "edge",
                "annotations": {"example.com/timeout": "3600"},
                "hosts": [
                    {
                        "host": "orch.example.test",
                        "paths": [{"path": "/", "pathType": "Prefix"}],
                    }
                ],
                "tls": [{"secretName": "orch-tls", "hosts": ["orch.example.test"]}],
            }
        },
    )
    spec = ing["spec"]
    assert spec["ingressClassName"] == "edge"
    assert ing["metadata"]["annotations"] == {"example.com/timeout": "3600"}
    assert spec["tls"] == [{"secretName": "orch-tls", "hosts": ["orch.example.test"]}]
    (rule,) = spec["rules"]
    assert rule["host"] == "orch.example.test"
    (path,) = rule["http"]["paths"]
    assert path["path"] == "/" and path["pathType"] == "Prefix"
    assert path["backend"]["service"] == {"name": FULL, "port": {"number": 80}}
    svc = next(d for d in _docs() if d["kind"] == "Service")
    assert svc["metadata"]["name"] == FULL and svc["spec"]["ports"][0]["port"] == 80


def test_values_have_no_homelab_defaults() -> None:
    lines = (CHART / "values.yaml").read_text().splitlines()
    for line in lines:
        code = line.split("#", 1)[0]
        if "szymonrichert.pl" in code:
            assert code.strip().startswith("repository:"), line
    for word in ["rook-ceph", "letsencrypt", "192.168.", "regcred", "whitelist-source-range"]:
        assert not any(word in line.split("#", 1)[0] for line in lines), word
    parsed = yaml.safe_load("\n".join(lines))
    assert not re.search(r"nginx", yaml.safe_dump(parsed["ingress"]))
