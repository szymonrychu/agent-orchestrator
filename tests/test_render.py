"""Renderer: golden objects, config validation against the wrapper, the onStart script."""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from claude_code_wrapper.config import load_config

from agent_orchestrator.db.clusters import Cluster
from agent_orchestrator.k8s import labels, on_start, render
from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec
from agent_orchestrator.tokens import ClusterTokens

GOLDEN = Path(__file__).parent / "golden"
TOKENS = ClusterTokens(auth="a" * 32, peer="p" * 32, agent="g" * 32)
AGENT_ENV = ["ORCH_AGENT_TOKEN", "ORCH_AGENT_URL", "ORCH_CLUSTER_ID", "POD_NAME"]
AGENT_URL = "http://orchestrator-agents.agents.svc:8081"
IMAGE = "harbor.example/containers/claude-code-wrapper:ca2ddf3"


def make_settings(**over) -> Settings:
    base = {
        "database_url": "postgresql://u:p@db/orch",
        "master_key": "m" * 32,
        "agent_image": IMAGE,
        "default_model": "sonnet",
        "models": ["sonnet", "claude-opus-5-5"],
    }
    base.update(over)
    return Settings(**base)


def make_cluster(slug="alpha", **spec) -> Cluster:
    return Cluster(
        id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        slug=slug,
        spec=ClusterSpec.model_validate(spec),
        generation=1,
        observed_generation=0,
        desired="running",
        status={},
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
    )


def by_kind(objs):
    return {o["kind"]: o for o in objs}


def config_of(objs) -> dict:
    return yaml.safe_load(by_kind(objs)["ConfigMap"]["data"]["config.yaml"])


def sts_of(objs) -> dict:
    return by_kind(objs)["StatefulSet"]


def container_of(objs) -> dict:
    return sts_of(objs)["spec"]["template"]["spec"]["containers"][0]


def golden(name: str, objs: list[dict]) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(yaml.safe_dump(objs, sort_keys=False))
    assert yaml.safe_load(path.read_text()) == objs


def test_golden_two_replicas_with_peers():
    cluster = make_cluster(
        replicas=2,
        env={"GITHUB_TOKEN": "x", "AAA": "1"},
        resources={"requests": {"cpu": "500m"}, "limits": {"memory": "2Gi"}},
        any_mcp_config={"servers": {}},
        pvc_size="5Gi",
        model="opus",
        effort="high",
    )
    golden("cluster_two_peers.yaml", render.render_cluster(cluster, make_settings(), TOKENS))


def test_golden_single_without_peers():
    cluster = make_cluster(slug="solo", replicas=1, peers=False)
    objs = render.render_cluster(cluster, make_settings(), TOKENS)
    assert "peers" not in config_of(objs)
    golden("cluster_single.yaml", objs)


def test_rendered_ccw_config_validates(tmp_path, monkeypatch):
    for peers in (True, False):
        objs = render.render_cluster(
            make_cluster(replicas=3, peers=peers, env={"FOO": "bar"}), make_settings(), TOKENS
        )
        monkeypatch.setenv("CCW_AUTH_TOKEN", TOKENS.auth)
        monkeypatch.setenv("CCW_PEER_TOKEN", TOKENS.peer)
        monkeypatch.setenv("CCW_PEER_URL", "http://agent-alpha-0.agent-alpha.agents.svc:8080")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth")
        monkeypatch.setenv("POD_NAME", "agent-alpha-0")
        path = tmp_path / "config.yaml"
        path.write_text(by_kind(objs)["ConfigMap"]["data"]["config.yaml"])
        cfg = load_config(path)
        assert cfg.hooks.onStart
        assert cfg.peers.enabled is peers
        assert cfg.claude.env.claude_autocompact_pct_override == 95


def test_all_objects_labelled():
    objs = render.render_cluster(make_cluster(), make_settings(), TOKENS)
    assert [o["kind"] for o in objs] == [
        "PersistentVolumeClaim",
        "Secret",
        "ConfigMap",
        "Service",
        "StatefulSet",
    ]
    for o in objs:
        assert o["metadata"]["labels"]["agents.szymonrichert.pl/cluster"] == "alpha"
        assert o["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "agent-orchestrator"


def test_sessions_subpathexpr_per_pod():
    mounts = {
        m["mountPath"]: m
        for m in container_of(
            render.render_cluster(make_cluster(workdir="proj"), make_settings(), TOKENS)
        )["volumeMounts"]
    }
    assert mounts["/sessions"]["subPathExpr"] == "sessions/$(POD_NAME)"
    assert mounts["/workdir"]["subPath"] == "workdir"
    assert "subPathExpr" not in mounts["/workdir"]
    assert mounts["/workdir"]["name"] == mounts["/sessions"]["name"]


def test_peer_static_urls_cover_every_ordinal():
    cfg = config_of(render.render_cluster(make_cluster(replicas=3), make_settings(), TOKENS))
    assert cfg["peers"]["enabled"] is True
    assert cfg["peers"]["name"] == "${POD_NAME}"
    assert cfg["peers"]["advertise_url"] == "${CCW_PEER_URL}"
    assert cfg["peers"]["token"] == "${CCW_PEER_TOKEN}"
    assert cfg["peers"]["static"] == [
        f"http://agent-alpha-{i}.agent-alpha.agents.svc.cluster.local:8080" for i in range(3)
    ]


def test_pull_secret_and_oauth_secret_ref():
    pod = sts_of(render.render_cluster(make_cluster(), make_settings(), TOKENS))["spec"][
        "template"
    ]["spec"]
    assert pod["imagePullSecrets"] == [{"name": "regcred"}]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "agent-orchestrator-claude",
        "key": "CLAUDE_CODE_OAUTH_TOKEN",
    }
    assert env["CCW_AUTH_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "agent-alpha-ccw",
        "key": "CCW_AUTH_TOKEN",
    }
    assert env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert env["CCW_PEER_URL"]["value"] == (
        "http://$(POD_NAME).agent-alpha.agents.svc.cluster.local:8080"
    )
    empty = sts_of(
        render.render_cluster(make_cluster(), make_settings(image_pull_secret=""), TOKENS)
    )
    assert "imagePullSecrets" not in empty["spec"]["template"]["spec"]
    custom = sts_of(
        render.render_cluster(make_cluster(), make_settings(claude_secret_name="mine"), TOKENS)
    )
    env = {e["name"]: e for e in custom["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["CLAUDE_CODE_OAUTH_TOKEN"]["valueFrom"]["secretKeyRef"]["name"] == "mine"


def test_any_mcp_server_entry():
    objs = render.render_cluster(make_cluster(), make_settings(), TOKENS)
    assert config_of(objs)["claude"]["mcp_servers"]["any-mcp"] == {
        "type": "stdio",
        "command": "/opt/any-mcp/bin/any-mcp",
        "args": ["--config", "/opt/any-mcp/etc/any-mcp.yaml"],
    }
    pod = sts_of(objs)["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in pod["volumes"]}
    mount = next(
        m for m in pod["containers"][0]["volumeMounts"] if m["mountPath"] == "/opt/any-mcp"
    )
    assert vols[mount["name"]]["emptyDir"] == {}
    mise = next(m for m in pod["containers"][0]["volumeMounts"] if m["mountPath"] == "/opt/mise")
    assert vols[mise["name"]]["emptyDir"] == {}


def test_config_hash_rolls_pods():
    def hash_of(cluster, **kw):
        objs = render.render_cluster(cluster, make_settings(**kw), TOKENS)
        return sts_of(objs)["spec"]["template"]["metadata"]["annotations"][
            "agents.szymonrichert.pl/config-hash"
        ]

    base = hash_of(make_cluster())
    assert hash_of(make_cluster()) == base
    assert hash_of(make_cluster(any_mcp_config={"a": 1})) != base
    assert hash_of(make_cluster(model="opus")) != base
    assert hash_of(make_cluster(), any_mcp_ref="v0.2.0") != base


def test_all_objects_in_namespace():
    objs = render.render_cluster(
        make_cluster(replicas=2), make_settings(namespace="ns1", cluster_domain="k.local"), TOKENS
    )
    for o in objs:
        assert o["metadata"]["namespace"] == "ns1"
    cfg = config_of(objs)
    assert all(".agent-alpha.ns1.svc.k.local:8080" in u for u in cfg["peers"]["static"])
    env = {e["name"]: e for e in container_of(objs)["env"]}
    assert ".agent-alpha.ns1.svc.k.local:8080" in env["CCW_PEER_URL"]["value"]
    assert sts_of(objs)["spec"]["serviceName"] == "agent-alpha"


def test_passthrough_env_lists_spec_env_names(tmp_path, monkeypatch):
    objs = render.render_cluster(
        make_cluster(env={"ZED": "1", "ALPHA": "2"}), make_settings(), TOKENS
    )
    cfg = config_of(objs)
    assert cfg["claude"]["passthrough_env"] == sorted(["ALPHA", "ZED", *AGENT_ENV])
    env = {e["name"]: e.get("value") for e in container_of(objs)["env"]}
    assert env["ZED"] == "1" and env["ALPHA"] == "2"
    for k, v in {
        "CCW_AUTH_TOKEN": "t" * 20,
        "CCW_PEER_TOKEN": "q" * 20,
        "CCW_PEER_URL": "http://x:1",
        "CLAUDE_CODE_OAUTH_TOKEN": "o",
        "POD_NAME": "p",
        "ORCH_AGENT_TOKEN": "g" * 20,
        "ORCH_AGENT_URL": "http://o:1",
        "ORCH_CLUSTER_ID": "c",
    }.items():
        monkeypatch.setenv(k, v)
    path = tmp_path / "c.yaml"
    path.write_text(by_kind(objs)["ConfigMap"]["data"]["config.yaml"])
    assert load_config(path).claude.passthrough_env == sorted(["ALPHA", "ZED", *AGENT_ENV])


def test_workdir_path_matches_mount_and_session_workdir():
    spec = ClusterSpec(workdir="proj")
    assert render.workdir_path(spec) == "/workdir/proj"
    mount = render.workdir_mount(spec)
    assert render.workdir_path(spec).startswith(mount["mountPath"] + "/")
    assert render.workdir_path(spec).removeprefix(mount["mountPath"] + "/") == spec.workdir
    objs = render.render_cluster(make_cluster(workdir="proj"), make_settings(), TOKENS)
    assert mount in container_of(objs)["volumeMounts"]
    assert mount["subPath"] == "workdir"


def test_every_object_name_uses_reserved_prefix():
    objs = render.render_cluster(make_cluster(slug="beta"), make_settings(), TOKENS)
    assert all(o["metadata"]["name"].startswith("agent-beta") for o in objs)
    names = labels.object_names("beta")
    assert set(names.values()) == {o["metadata"]["name"] for o in objs}
    assert labels.cluster_labels("beta") == {
        "agents.szymonrichert.pl/cluster": "beta",
        "app.kubernetes.io/managed-by": "agent-orchestrator",
    }
    assert labels.selector("beta") == {"matchLabels": labels.cluster_labels("beta")}


def test_on_start_hook_and_startup_probe():
    objs = render.render_cluster(
        make_cluster(), make_settings(on_start_timeout_seconds=600), TOKENS
    )
    assert config_of(objs)["hooks"]["onStart"] == [
        {
            "type": "command",
            "run": "sh /etc/ccw/on-start.sh",
            "blocking": True,
            "on_error": "deny",
            "timeout_seconds": 600,
        }
    ]
    assert "on-start.sh" in by_kind(objs)["ConfigMap"]["data"]
    c = container_of(objs)
    assert c["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert c["startupProbe"]["periodSeconds"] == 5
    assert c["startupProbe"]["failureThreshold"] == 600 // 5 + 12
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"
    odd = container_of(
        render.render_cluster(make_cluster(), make_settings(on_start_timeout_seconds=601), TOKENS)
    )
    assert odd["startupProbe"]["failureThreshold"] == 121 + 12


def _stub_bin(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    root = tmp_path / "any-mcp"
    python3 = bin_dir / "python3"
    python3.write_text(
        f"""#!/bin/sh
echo "python3 $*" >> {log}
if [ "$1 $2" = "-m venv" ]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\\necho "pip $*" >> {log}\\n' > "$3/bin/pip"
  printf '#!/bin/sh\\necho "any-mcp $*" >> {log}\\n' > "$3/bin/any-mcp"
  chmod +x "$3/bin/pip" "$3/bin/any-mcp"
fi
"""
    )
    python3.chmod(python3.stat().st_mode | stat.S_IEXEC)
    return log, root


def _run(script: str, root: Path, tmp_path: Path) -> None:
    path = tmp_path / "on-start.sh"
    path.write_text(script.replace("/opt/any-mcp", str(root)))
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"}
    subprocess.run(["sh", str(path)], check=True, env=env, capture_output=True)


def test_on_start_script_installs_once_and_writes_json_config(tmp_path):
    settings = make_settings(any_mcp_ref="v0.1.0", any_mcp_repo="https://example.com/any-mcp")
    spec = ClusterSpec(any_mcp_config={"name": "it's $HOME `x`", "n": [1, 2]})
    script = on_start.render_on_start(spec, settings)
    assert script.startswith("#!/bin/sh\n")
    assert "set -eu" in script
    log, root = _stub_bin(tmp_path)
    _run(script, root, tmp_path)
    calls = log.read_text()
    assert "pip install --no-cache-dir any-mcp @ git+https://example.com/any-mcp@v0.1.0" in calls
    assert calls.count("pip install") == 1
    assert "any-mcp --help" in calls
    assert (root / ".ref").read_text().strip() == "v0.1.0"
    assert json.loads((root / "etc" / "any-mcp.yaml").read_text()) == spec.any_mcp_config

    log.write_text("")
    spec2 = ClusterSpec(any_mcp_config={"changed": True})
    _run(on_start.render_on_start(spec2, settings), root, tmp_path)
    calls = log.read_text()
    assert "pip install" not in calls and "-m venv" not in calls
    assert "any-mcp --help" in calls
    assert json.loads((root / "etc" / "any-mcp.yaml").read_text()) == {"changed": True}

    log.write_text("")
    _run(on_start.render_on_start(spec2, make_settings(any_mcp_ref="v0.2.0")), root, tmp_path)
    assert log.read_text().count("pip install") == 1
    assert (root / ".ref").read_text().strip() == "v0.2.0"


def test_on_start_config_sources(tmp_path):
    f = tmp_path / "mcp.yaml"
    f.write_text("a: 1\n")
    settings = make_settings(any_mcp_config_file=str(f))
    assert '"a": 1' in on_start.render_on_start(ClusterSpec(), settings)
    spec = ClusterSpec(any_mcp_config={"b": 2})
    assert '"b": 2' in on_start.render_on_start(spec, settings)
    assert "\n{}\n" in on_start.render_on_start(ClusterSpec(), make_settings())


def test_on_start_refuses_delimiter_in_config(monkeypatch):
    monkeypatch.setattr(on_start, "DELIMITER", "}")
    with pytest.raises(ValueError, match="delimiter"):
        on_start.render_on_start(ClusterSpec(any_mcp_config={"a": 1}), make_settings())


def test_ask_user_question_disabled():
    cfg = config_of(render.render_cluster(make_cluster(), make_settings(), TOKENS))
    assert cfg["sessions"]["ask_user_question"] == "deny"
    assert cfg["claude"]["permissions"]["deny"] == [
        "EnterPlanMode",
        "ExitPlanMode",
        "AskUserQuestion",
    ]
    assert cfg["sessions"]["max_sessions"] == 1
    assert cfg["sessions"]["resume_on_start"] is True
    assert cfg["sessions"]["idle_timeout_seconds"] == 0


def test_system_append_asks_in_writing():
    cfg = config_of(render.render_cluster(make_cluster(), make_settings(), TOKENS))
    text = cfg["prompts"]["system_append"]
    assert text == render.ASK_IN_WRITING_PROMPT
    low = text.lower()
    assert "numbered list" in low
    assert "recommended answer" in low
    assert '"yes"' in text
    assert "askuserquestion" in low


def test_shutdown_below_grace_and_resources():
    objs = render.render_cluster(
        make_cluster(resources={"limits": {"cpu": "1"}}),
        make_settings(agent_termination_grace_seconds=90),
        TOKENS,
    )
    pod = sts_of(objs)["spec"]["template"]["spec"]
    assert pod["terminationGracePeriodSeconds"] == 90
    assert config_of(objs)["limits"]["shutdown_timeout_seconds"] < 90
    assert pod["containers"][0]["resources"] == {"limits": {"cpu": "1"}}
    assert pod["securityContext"] == {"runAsUser": 1000, "fsGroup": 1000}


def test_render_is_pure():
    cluster = make_cluster(replicas=2)
    a = render.render_cluster(cluster, make_settings(), TOKENS)
    assert render.render_cluster(cluster, make_settings(), TOKENS) == copy.deepcopy(a)


def test_render_keeps_spec_resources_unchanged():
    c = container_of(render.render_cluster(make_cluster(), make_settings(), TOKENS))
    assert "resources" not in c
    spec = {"resources": {"requests": {"cpu": "2"}, "limits": {"memory": "2Gi"}}}
    c = container_of(render.render_cluster(make_cluster(**spec), make_settings(), TOKENS))
    assert c["resources"] == {"requests": {"cpu": "2"}, "limits": {"memory": "2Gi"}}


def test_agent_credential_env():
    cluster = make_cluster()
    objs = render.render_cluster(cluster, make_settings(agent_api_url=AGENT_URL), TOKENS)
    assert by_kind(objs)["Secret"]["stringData"]["ORCH_AGENT_TOKEN"] == TOKENS.agent
    env = {e["name"]: e for e in container_of(objs)["env"]}
    assert env["ORCH_AGENT_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "agent-alpha-ccw",
        "key": "ORCH_AGENT_TOKEN",
    }
    assert env["ORCH_CLUSTER_ID"]["value"] == str(cluster.id)
    assert env["ORCH_AGENT_URL"]["value"] == AGENT_URL


def test_passthrough_includes_agent_env():
    objs = render.render_cluster(
        make_cluster(env={"ZED": "1"}), make_settings(agent_api_url=AGENT_URL), TOKENS
    )
    assert config_of(objs)["claude"]["passthrough_env"] == sorted(["ZED", *AGENT_ENV])
    solo = render.render_cluster(
        make_cluster(replicas=1), make_settings(agent_api_url=AGENT_URL), TOKENS
    )
    assert "ORCH_AGENT_TOKEN" in config_of(solo)["claude"]["passthrough_env"]


def test_no_agent_url_omits_env():
    objs = render.render_cluster(make_cluster(), make_settings(), TOKENS)
    names = [e["name"] for e in container_of(objs)["env"]]
    assert "ORCH_AGENT_URL" not in names
    assert "ORCH_AGENT_TOKEN" in names and "ORCH_CLUSTER_ID" in names
    assert "ORCH_AGENT_URL" in config_of(objs)["claude"]["passthrough_env"]


def test_agent_token_trust_domain_documented():
    """The token is plain pod env, readable by every process in the pod (Bash included)."""
    objs = render.render_cluster(make_cluster(), make_settings(agent_api_url=AGENT_URL), TOKENS)
    env = {e["name"]: e for e in container_of(objs)["env"]}
    assert "ORCH_AGENT_TOKEN" in env
    assert "ORCH_AGENT_TOKEN" in config_of(objs)["claude"]["passthrough_env"]
    readme = (Path(__file__).parent.parent / "README.md").read_text().lower()
    assert "best-effort" in readme and "orch_agent_token" in readme
