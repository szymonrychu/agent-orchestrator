"""Workspace cache values of the chart: generic S3, credentials on the reconciler (D2, D8)."""

from __future__ import annotations

import copy
from typing import Any

import yaml
from helm_render import CHART, LINT_VALUES, lint, render, render_error

FULL = "agents-orchestrator"
COMPONENTS = ["api", "dispatcher", "ingester", "reconciler"]
REPO = "harbor.szymonrichert.pl/containers/agents-orchestrator"
BASE: dict[str, Any] = {
    "agentImage": "registry.example/wrapper:1",
    "masterKey": {"value": "mk"},
    "claude": {"token": "tok"},
    "postgres": {"url": "postgresql://u:p@db:5432/app"},
}
CREDENTIAL_ENV = ("ORCH_CACHE_S3_ACCESS_KEY_ID", "ORCH_CACHE_S3_SECRET_ACCESS_KEY")
NON_SECRET_ENV = (
    "ORCH_CACHE_S3_ENDPOINT",
    "ORCH_CACHE_S3_REGION",
    "ORCH_CACHE_S3_BUCKET",
    "ORCH_CACHE_S3_PREFIX",
    "ORCH_CACHE_S3_PATH_STYLE",
    "ORCH_CACHE_S3_VERIFY_TLS",
    "ORCH_CACHE_IMAGE",
    "ORCH_CACHE_MAX_BYTES",
    "ORCH_CACHE_TIMEOUT_SECONDS",
)


def _values(cache: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    v = copy.deepcopy(BASE)
    if cache is not None:
        v["cache"] = cache
    v.update(extra)
    return v


def _cache(**s3: Any) -> dict[str, Any]:
    return {"s3": {"bucket": "b", "existingSecret": "s", **s3}}


def _settings(docs: list[dict]) -> dict[str, str]:
    (cm,) = [
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == f"{FULL}-settings"
    ]
    return cm["data"]


def _deployments(docs: list[dict]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}


def _env_names(container: dict) -> list[str]:
    return [e["name"] for e in container.get("env", [])]


def test_configmap_cache_env() -> None:
    data = _settings(render(_values(_cache())))
    assert {k: data[k] for k in NON_SECRET_ENV} == {
        "ORCH_CACHE_S3_ENDPOINT": "",
        "ORCH_CACHE_S3_REGION": "us-east-1",
        "ORCH_CACHE_S3_BUCKET": "b",
        "ORCH_CACHE_S3_PREFIX": "workspace-cache/",
        "ORCH_CACHE_S3_PATH_STYLE": "false",
        "ORCH_CACHE_S3_VERIFY_TLS": "true",
        "ORCH_CACHE_IMAGE": f"{REPO}:{_app_version()}",
        "ORCH_CACHE_MAX_BYTES": "5368709120",
        "ORCH_CACHE_TIMEOUT_SECONDS": "1800",
    }
    custom = {
        "s3": {
            "endpoint": "https://s3.example.com",
            "region": "eu-central-1",
            "bucket": "cache",
            "prefix": "ws/",
            "pathStyle": True,
            "verifyTls": False,
            "existingSecret": "s",
        },
        "image": "registry.example/cache:2",
        "maxBytes": 1073741824,
        "timeoutSeconds": 600,
    }
    data = _settings(render(_values(custom)))
    assert {k: data[k] for k in NON_SECRET_ENV} == {
        "ORCH_CACHE_S3_ENDPOINT": "https://s3.example.com",
        "ORCH_CACHE_S3_REGION": "eu-central-1",
        "ORCH_CACHE_S3_BUCKET": "cache",
        "ORCH_CACHE_S3_PREFIX": "ws/",
        "ORCH_CACHE_S3_PATH_STYLE": "true",
        "ORCH_CACHE_S3_VERIFY_TLS": "false",
        "ORCH_CACHE_IMAGE": "registry.example/cache:2",
        "ORCH_CACHE_MAX_BYTES": "1073741824",
        "ORCH_CACHE_TIMEOUT_SECONDS": "600",
    }
    assert not set(CREDENTIAL_ENV) & set(data)


def test_no_cache_no_env() -> None:
    docs = render(_values())
    assert not [k for k in _settings(docs) if k.startswith("ORCH_CACHE_")]
    for dep in _deployments(docs).values():
        spec = dep["spec"]["template"]["spec"]
        for cont in spec["containers"] + spec["initContainers"]:
            assert not [n for n in _env_names(cont) if n.startswith("ORCH_CACHE_")]


def test_credentials_only_on_reconciler() -> None:
    cache = _cache(existingSecret="cache-creds", accessKeyIdKey="id", secretAccessKeyKey="key")
    docs = render(_values(cache))
    deps = _deployments(docs)
    assert sorted(deps) == sorted(f"{FULL}-{c}" for c in COMPONENTS)
    reconciler = deps[f"{FULL}-reconciler"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in reconciler["env"]}
    assert env["ORCH_CACHE_S3_ACCESS_KEY_ID"]["valueFrom"] == {
        "secretKeyRef": {"name": "cache-creds", "key": "id"}
    }
    assert env["ORCH_CACHE_S3_SECRET_ACCESS_KEY"]["valueFrom"] == {
        "secretKeyRef": {"name": "cache-creds", "key": "key"}
    }
    assert _env_names(reconciler).count("ORCH_CACHE_S3_ACCESS_KEY_ID") == 1
    for name, dep in deps.items():
        spec = dep["spec"]["template"]["spec"]
        for init in spec["initContainers"]:
            assert not set(CREDENTIAL_ENV) & set(_env_names(init)), name
        if name != f"{FULL}-reconciler":
            assert not set(CREDENTIAL_ENV) & set(_env_names(spec["containers"][0])), name
    # Nothing the chart renders besides the reconciler Deployment names the credential
    # Secret: the agent pods read the settings ConfigMap, which carries no credential.
    others = [d for d in docs if d["metadata"]["name"] != f"{FULL}-reconciler"]
    assert "cache-creds" not in yaml.safe_dump_all(others)
    assert not set(CREDENTIAL_ENV) & set(_settings(docs))


def test_credentials_default_key_names() -> None:
    deps = _deployments(render(_values(_cache())))
    reconciler = deps[f"{FULL}-reconciler"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in reconciler["env"]}
    assert env["ORCH_CACHE_S3_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"] == {
        "name": "s",
        "key": "AWS_ACCESS_KEY_ID",
    }
    assert env["ORCH_CACHE_S3_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "s",
        "key": "AWS_SECRET_ACCESS_KEY",
    }


def test_bucket_requires_secret() -> None:
    err = render_error(_values({"s3": {"bucket": "b"}}))
    assert "cache.s3.existingSecret" in err
    err = render_error(
        _values({"s3": {"bucket": "b"}}),
        show_only=["templates/deployment.yaml"],
    )
    assert "cache.s3.existingSecret" in err


def test_cache_image_defaults_to_chart_image() -> None:
    assert _settings(render(_values(_cache())))["ORCH_CACHE_IMAGE"] == f"{REPO}:{_app_version()}"
    tagged = _values(_cache(), image={"repository": "registry.example/orch", "tag": "1.2.3"})
    assert _settings(render(tagged))["ORCH_CACHE_IMAGE"] == "registry.example/orch:1.2.3"
    deps = _deployments(render(tagged))
    main = deps[f"{FULL}-reconciler"]["spec"]["template"]["spec"]["containers"][0]
    assert main["image"] == "registry.example/orch:1.2.3"


def test_reconciler_role_manages_cache_jobs() -> None:
    docs = render(_values(_cache()))
    (role,) = [
        d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == f"{FULL}-reconciler"
    ]
    assert role["metadata"]["namespace"] == "agents"
    assert not [d for d in docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    jobs = [r for r in role["rules"] if r["apiGroups"] == ["batch"] and "jobs" in r["resources"]]
    assert jobs and {"create", "get", "delete"} <= set(jobs[0]["verbs"])


def test_cache_values_generic_s3() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["cache"] == {
        "s3": {
            "endpoint": "",
            "region": "us-east-1",
            "bucket": "",
            "prefix": "workspace-cache/",
            "pathStyle": False,
            "verifyTls": True,
            "existingSecret": "",
            "accessKeyIdKey": "AWS_ACCESS_KEY_ID",
            "secretAccessKeyKey": "AWS_SECRET_ACCESS_KEY",
        },
        "image": "",
        "maxBytes": 5368709120,
        "timeoutSeconds": 1800,
    }
    for path in (CHART / "values.yaml", CHART / "README.md"):
        text = path.read_text().lower()
        assert "rook" not in text and "ceph" not in text, path


def test_lint_values_render_cache() -> None:
    lint_values = yaml.safe_load(LINT_VALUES.read_text())
    assert lint_values["cache"]["s3"]["bucket"]
    data = _settings(render(lint_values))
    assert data["ORCH_CACHE_S3_BUCKET"] == lint_values["cache"]["s3"]["bucket"]
    proc = lint()
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_readme_documents_cache_values() -> None:
    text = (CHART / "README.md").read_text()
    for key in (
        "cache.s3.endpoint",
        "cache.s3.region",
        "cache.s3.bucket",
        "cache.s3.prefix",
        "cache.s3.pathStyle",
        "cache.s3.verifyTls",
        "cache.s3.existingSecret",
        "cache.s3.accessKeyIdKey",
        "cache.s3.secretAccessKeyKey",
        "cache.image",
        "cache.maxBytes",
        "cache.timeoutSeconds",
    ):
        assert f"`{key}`" in text, key


def _app_version() -> str:
    return str(yaml.safe_load((CHART / "Chart.yaml").read_text())["appVersion"])
