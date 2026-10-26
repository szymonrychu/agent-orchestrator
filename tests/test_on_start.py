"""The orchestrator plugin entry merged into the rendered any-mcp config."""

from __future__ import annotations

import json
import re

import pytest
from any_mcp.config import AnyMcpConfig
from any_mcp.orchestrator import OrchestratorPlugin

from agents_orchestrator.k8s import on_start
from agents_orchestrator.settings import Settings
from agents_orchestrator.spec import SessionSpec

HIVE_TOOLS = [
    "me",
    "leadership_handover",
    "lock_claim",
    "lock_release",
    "lock_list",
    "lock_reassign",
]
SESSION_TOOLS = ["sessions_list", "session_send"]
SECRET = "s3cr3t-token-value"


def make_settings(**over) -> Settings:
    base = {
        "database_url": "postgresql://u:p@db/orch",
        "master_key": "m" * 32,
        "agent_image": "img:1",
        "default_model": "sonnet",
        "models": ["sonnet"],
        "agent_api_url": "http://orch.example:8080",
    }
    base.update(over)
    return Settings(**base)


def written_config(script: str) -> dict:
    m = re.search(r"<<'ANY_MCP_CONFIG_EOF'\n(.*)\nANY_MCP_CONFIG_EOF\n", script, re.S)
    assert m
    return json.loads(m.group(1))


def entries(spec: SessionSpec, settings: Settings | None = None) -> dict:
    cfg = written_config(on_start.render_on_start(spec, settings or make_settings(), "alpha"))
    return {p["name"]: p for p in cfg.get("plugins", [])}


@pytest.mark.parametrize("messaging", [None, False, True])
@pytest.mark.parametrize("static", [False, True])
def test_alone_session_tools_entry(messaging, static):
    """D11: an alone session gets only the session tools, whatever session_messaging says."""
    spec = SessionSpec(replicas=1, static=static, session_messaging=messaging)
    assert on_start.orchestrator_allow(spec) == SESSION_TOOLS
    entry = entries(spec)["orchestrator"]
    assert entry["allow"] == SESSION_TOOLS
    assert entry["config"]["hive"] is False
    assert entry["config"]["max_replicas"] == 1
    cfg = written_config(on_start.render_on_start(spec, make_settings(), "alpha"))
    config = AnyMcpConfig.model_validate(cfg)
    plugin = next(p for p in config.plugins if p.name == "orchestrator")
    OrchestratorPlugin.Config.model_validate(plugin.config)


@pytest.mark.parametrize("messaging", [None, False, True])
@pytest.mark.parametrize(
    "spec_fields,hive_allow",
    [
        ({"replicas": 2, "static": True}, HIVE_TOOLS),
        ({"replicas": 2}, [*HIVE_TOOLS, "scale"]),
        ({"replicas": 1, "max_replicas": 3}, [*HIVE_TOOLS, "scale"]),
    ],
)
def test_hive_session_tools(messaging, spec_fields, hive_allow):
    """D11: hive tools, scale when scalable, then the session tools, whatever session_messaging."""
    spec = SessionSpec(session_messaging=messaging, **spec_fields)
    assert on_start.orchestrator_allow(spec) == [*hive_allow, *SESSION_TOOLS]
    assert entries(spec)["orchestrator"]["allow"] == [*hive_allow, *SESSION_TOOLS]


def test_static_hive_has_no_scale_tool():
    spec = SessionSpec(replicas=2, static=True)
    assert on_start.orchestrator_allow(spec) == [*HIVE_TOOLS, *SESSION_TOOLS]


def test_hive_entry_and_allow():
    spec = SessionSpec(replicas=2, max_replicas=4, auto_compact_pct=70)
    assert on_start.orchestrator_allow(spec) == [*HIVE_TOOLS, "scale", *SESSION_TOOLS]
    entry = entries(spec)["orchestrator"]
    assert entry == {
        "name": "orchestrator",
        "plugin": "any_mcp.orchestrator:OrchestratorPlugin",
        "namespace": "orchestrator",
        "allow": [*HIVE_TOOLS, "scale", *SESSION_TOOLS],
        "config": {
            "base_url": "${ORCH_AGENT_URL}",
            "token": "${ORCH_AGENT_TOKEN}",
            "session": "${ORCH_SESSION_ID}",
            "agent": "${POD_NAME}",
            "slug": "alpha",
            "max_replicas": 4,
            "static": False,
            "hive": True,
            "auto_compact_pct": 70,
        },
    }
    cfg = written_config(on_start.render_on_start(spec, make_settings(), "alpha"))
    AnyMcpConfig.model_validate(cfg)


def test_plugin_entry_session_key():
    """The rendered entry names the session and validates under the installed any-mcp."""
    spec = SessionSpec(replicas=2, max_replicas=4)
    cfg = written_config(on_start.render_on_start(spec, make_settings(), "alpha"))
    config = AnyMcpConfig.model_validate(cfg)
    entry = next(p for p in config.plugins if p.name == "orchestrator")
    assert entry.config["session"] == "${ORCH_SESSION_ID}"
    plugin_config = OrchestratorPlugin.Config.model_validate(entry.config)
    assert plugin_config.session == "${ORCH_SESSION_ID}"


def test_user_config_merged():
    user = {"plugins": [{"name": "gh", "plugin": "x:Y", "config": {"a": 1}}], "tools": {}}
    spec = SessionSpec(replicas=2, any_mcp_config=user)
    got = entries(spec)
    assert got["gh"] == user["plugins"][0]
    assert "orchestrator" in got
    assert spec.any_mcp_config == {
        "plugins": [{"name": "gh", "plugin": "x:Y", "config": {"a": 1}}],
        "tools": {},
    }
    cfg = written_config(on_start.render_on_start(spec, make_settings(), "alpha"))
    assert cfg["tools"] == {}
    AnyMcpConfig.model_validate(cfg)


def test_default_config_used_when_spec_has_none(tmp_path):
    f = tmp_path / "mcp.yaml"
    f.write_text("plugins:\n  - {name: gh, plugin: 'x:Y'}\n")
    got = entries(SessionSpec(replicas=2), make_settings(any_mcp_config_file=str(f)))
    assert set(got) == {"gh", "orchestrator"}


@pytest.mark.parametrize("spec", [SessionSpec(replicas=1), SessionSpec(replicas=2)])
def test_omitted_without_agent_api_url_or_slug(spec):
    assert "orchestrator" in entries(spec)
    assert "orchestrator" not in entries(spec, make_settings(agent_api_url=None))
    for slug in (None, ""):
        cfg = written_config(on_start.render_on_start(spec, make_settings(), slug))
        assert "orchestrator" not in {p["name"] for p in cfg.get("plugins", [])}


def test_placeholders_not_secrets():
    spec = SessionSpec(replicas=2)
    script = on_start.render_on_start(spec, make_settings(master_key=SECRET * 2), "alpha")
    assert "${ORCH_AGENT_TOKEN}" in script and "${POD_NAME}" in script
    assert SECRET not in script


def test_invariant_to_replicas():
    a = on_start.render_on_start(SessionSpec(replicas=2, max_replicas=4), make_settings(), "alpha")
    b = on_start.render_on_start(SessionSpec(replicas=3, max_replicas=4), make_settings(), "alpha")
    assert a == b


def test_user_orchestrator_entry_replaced():
    user = {"plugins": [{"name": "orchestrator", "plugin": "x:Y"}, {"name": "gh", "plugin": "x:Z"}]}
    spec = SessionSpec(replicas=2, any_mcp_config=user)
    cfg = written_config(on_start.render_on_start(spec, make_settings(), "alpha"))
    names = [p["name"] for p in cfg["plugins"]]
    assert names.count("orchestrator") == 1 and "gh" in names
    assert next(p for p in cfg["plugins"] if p["name"] == "orchestrator")["plugin"] == (
        "any_mcp.orchestrator:OrchestratorPlugin"
    )
    AnyMcpConfig.model_validate(cfg)
