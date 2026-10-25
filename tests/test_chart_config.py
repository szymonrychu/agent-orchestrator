"""Settings, Secrets and the Postgres connection of the chart (D28, D7, D15)."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from helm_render import CHART, LINT_VALUES, render, render_error

FULL = "agents-orchestrator"
BASE: dict[str, Any] = {
    "agentImage": "registry.example/wrapper:1",
    "masterKey": {"value": "mk"},
    "claude": {"token": "tok"},
}
URL = "postgresql://u:p@db:5432/app"


@pytest.fixture
def env_chart(tmp_path: Path) -> Path:
    """A chart copy with a probe template that renders the shared container env."""
    dst = tmp_path / "agents-orchestrator"
    shutil.copytree(CHART, dst)
    (dst / "templates" / "env-probe.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: env-probe\ndata:\n"
        "  env: |\n"
        '    {{- include "agents-orchestrator.env" . | nindent 4 }}\n'
    )
    return dst


def values(**extra: Any) -> dict[str, Any]:
    v = copy.deepcopy(BASE)
    v.update(extra)
    return v


def by_kind(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def test_requires_postgres_connection():
    err = render_error(values())
    assert (
        "agents-orchestrator: a Postgres connection is required: set postgres.url, "
        "postgres.host/user/database/password, postgres.existingSecret, or cnpg.enabled=true" in err
    )


def test_postgres_url_and_parts():
    secrets = by_kind(render(values(postgres={"url": URL})), "Secret")
    assert yaml.safe_load(yaml.safe_dump(secrets[f"{FULL}-database"]["stringData"])) == {"uri": URL}

    parts = {
        "host": "db.example",
        "user": "us er@x",
        "password": "p:w/d?#",
        "database": "orch",
        "port": 6432,
        "sslmode": "require",
    }
    secrets = by_kind(render(values(postgres=parts)), "Secret")
    assert secrets[f"{FULL}-database"]["stringData"]["uri"] == (
        "postgresql://us%20er%40x:p%3Aw%2Fd%3F%23@db.example:6432/orch?sslmode=require"
    )
    parts.pop("sslmode")
    parts["password"] = ""
    secrets = by_kind(render(values(postgres=parts)), "Secret")
    assert secrets[f"{FULL}-database"]["stringData"]["uri"] == (
        "postgresql://us%20er%40x:@db.example:6432/orch"
    )


def test_postgres_parts_incomplete_is_not_a_source():
    err = render_error(values(postgres={"host": "db", "user": "u"}))
    assert "a Postgres connection is required" in err


def test_postgres_url_wired_from_chart_secret(env_chart):
    docs = render(values(postgres={"url": URL}), chart=env_chart)
    (probe,) = [d for d in docs if d["metadata"]["name"] == "env-probe"]
    block = yaml.safe_load(probe["data"]["env"])
    env = block["env"]
    db = next(e for e in env if e["name"] == "ORCH_DATABASE_URL")
    assert db["valueFrom"]["secretKeyRef"] == {"name": f"{FULL}-database", "key": "uri"}


def test_postgres_existing_secret_ref(env_chart):
    docs = render(
        values(postgres={"existingSecret": "pg-secret", "existingSecretKey": "dsn"}),
        chart=env_chart,
    )
    assert f"{FULL}-database" not in by_kind(docs, "Secret")
    (probe,) = [d for d in docs if d["metadata"]["name"] == "env-probe"]
    block = yaml.safe_load(probe["data"]["env"])
    env = block["env"]
    db = next(e for e in env if e["name"] == "ORCH_DATABASE_URL")
    assert db["valueFrom"]["secretKeyRef"] == {"name": "pg-secret", "key": "dsn"}
    mk = next(e for e in env if e["name"] == "ORCH_MASTER_KEY")
    assert mk["valueFrom"]["secretKeyRef"] == {"name": f"{FULL}-master-key", "key": "master-key"}
    assert block["envFrom"] == [{"configMapRef": {"name": f"{FULL}-settings"}}]


def test_multiple_postgres_sources_rejected():
    err = render_error(values(postgres={"url": URL, "existingSecret": "x"}))
    assert "postgres.url" in err and "postgres.existingSecret" in err
    err = render_error(
        values(
            postgres={"url": URL, "host": "h", "user": "u", "database": "d"}, cnpg={"enabled": True}
        )
    )
    assert "postgres.url" in err and "postgres.host/user/database" in err and "cnpg.enabled" in err


def test_cnpg_optional_with_backup_passthrough(env_chart):
    docs = render(values(postgres={"url": URL}))
    assert "Cluster" not in {d["kind"] for d in docs}

    backup = {
        "retentionPolicy": "30d",
        "barmanObjectStore": {"destinationPath": "s3://b/p", "s3Credentials": {"x": {"name": "s"}}},
    }
    cnpg = {
        "enabled": True,
        "instances": 3,
        "imageName": "ghcr.io/cloudnative-pg/postgresql:17",
        "storage": {"size": "20Gi", "storageClass": "fast"},
        "walStorage": {"enabled": True, "size": "4Gi", "storageClass": "wal"},
        "resources": {"limits": {"memory": "1Gi"}},
        "backup": backup,
    }
    docs = render(values(cnpg=cnpg), chart=env_chart)
    (cluster,) = [d for d in docs if d["kind"] == "Cluster"]
    assert cluster["apiVersion"] == "postgresql.cnpg.io/v1"
    assert cluster["metadata"]["name"] == f"{FULL}-db"
    spec = cluster["spec"]
    assert spec["instances"] == 3
    assert spec["imageName"] == "ghcr.io/cloudnative-pg/postgresql:17"
    assert spec["storage"] == {"size": "20Gi", "storageClass": "fast"}
    assert spec["walStorage"] == {"size": "4Gi", "storageClass": "wal"}
    assert spec["resources"] == {"limits": {"memory": "1Gi"}}
    assert spec["bootstrap"]["initdb"] == {
        "database": "agents_orchestrator",
        "owner": "agents_orchestrator",
    }
    assert spec["backup"] == backup
    assert f"{FULL}-database" not in by_kind(docs, "Secret")
    (probe,) = [d for d in docs if d["metadata"]["name"] == "env-probe"]
    block = yaml.safe_load(probe["data"]["env"])
    env = block["env"]
    db = next(e for e in env if e["name"] == "ORCH_DATABASE_URL")
    assert db["valueFrom"]["secretKeyRef"] == {"name": f"{FULL}-db-app", "key": "uri"}

    minimal = render(values(cnpg={"enabled": True}))
    (cluster,) = [d for d in minimal if d["kind"] == "Cluster"]
    assert "backup" not in cluster["spec"]
    assert "walStorage" not in cluster["spec"]
    assert "imageName" not in cluster["spec"]
    assert "storageClass" not in cluster["spec"]["storage"]


def test_settings_configmap():
    cm = by_kind(
        render(
            values(postgres={"url": URL}, env={"ORCH_LOG_LEVEL": "debug", "ORCH_X": 5}),
            namespace="ns1",
        ),
        "ConfigMap",
    )[f"{FULL}-settings"]
    assert cm["data"] == {
        "ORCH_NAMESPACE": "ns1",
        "ORCH_AGENT_IMAGE": "registry.example/wrapper:1",
        "ORCH_AGENT_PORT": "8001",
        "ORCH_SERVICE_PORT": "8002",
        "ORCH_AGENT_API_URL": f"http://{FULL}-agents.ns1.svc:8001",
        "ORCH_CLAUDE_SECRET_NAME": f"{FULL}-claude",
        "ORCH_LOG_LEVEL": "debug",
        "ORCH_X": "5",
    }
    cm = by_kind(
        render(
            values(postgres={"url": URL}, claude={"existingSecret": "mine"}),
        ),
        "ConfigMap",
    )[f"{FULL}-settings"]
    assert cm["data"]["ORCH_CLAUDE_SECRET_NAME"] == "mine"

    err = render_error(values(postgres={"url": URL}, env={"LOG_LEVEL": "x"}))
    assert "LOG_LEVEL" in err and "ORCH_" in err

    err = render_error({**values(postgres={"url": URL}), "agentImage": ""})
    assert "agentImage" in err


def test_master_key_and_claude_secrets():
    docs = render(values(postgres={"url": URL}))
    secrets = by_kind(docs, "Secret")
    assert secrets[f"{FULL}-master-key"]["stringData"] == {"master-key": "mk"}
    assert secrets[f"{FULL}-claude"]["stringData"] == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}

    docs = render(
        values(
            postgres={"url": URL},
            masterKey={"existingSecret": "mk-s", "existingSecretKey": "k"},
            claude={"existingSecret": "cl-s"},
        )
    )
    secrets = by_kind(docs, "Secret")
    assert f"{FULL}-master-key" not in secrets and f"{FULL}-claude" not in secrets

    err = render_error({**values(postgres={"url": URL}), "masterKey": {}})
    assert "masterKey" in err
    err = render_error({**values(postgres={"url": URL}), "claude": {}})
    assert "claude" in err


def test_no_secret_values_in_configmap():
    # built at run time so no secret-shaped literal sits in the source
    pw, mk, tok = ("".join(["marker", name, "value"]) for name in ("pw", "mk", "tok"))
    docs = render(
        values(
            postgres={"host": "h", "user": "u", "password": pw, "database": "d"},
            masterKey={"value": mk},
            claude={"token": tok},
        )
    )
    for cm in by_kind(docs, "ConfigMap").values():
        text = yaml.safe_dump(cm)
        for secret in (pw, mk, tok, "postgresql://"):
            assert secret not in text


def test_secret_names_never_match_agent_pod_secrets():
    import re

    docs = render(values(postgres={"url": URL}), release="agent-x-ccw")
    for name in by_kind(docs, "Secret"):
        assert not re.fullmatch(r"agent-.+-ccw", name), name


def test_lint_values_render():
    assert render(yaml.safe_load(LINT_VALUES.read_text()))
