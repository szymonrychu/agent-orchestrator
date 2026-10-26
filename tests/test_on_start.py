"""The orchestrator plugin entry merged into the rendered any-mcp config."""

from __future__ import annotations

import json
import re

import pytest
from any_mcp.config import AnyMcpConfig
from any_mcp_tatara_orchestrator import OrchestratorPlugin

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
INIT_SCRIPTS_TOOLS = ["init_scripts_get", "init_scripts_edit"]
TAIL_TOOLS = [*SESSION_TOOLS, *INIT_SCRIPTS_TOOLS]
SECRET = "s3cr3t-token-value"
PLUGINS_REPO = "https://github.com/szymonrychu/any-mcp-tatara-plugins"
PLUGINS_REF = Settings.model_fields["any_mcp_plugins_ref"].default
RESERVED_SOURCE = f"git::{PLUGINS_REPO}//orchestration/orchestrator?ref={PLUGINS_REF}"


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
    assert on_start.orchestrator_allow(spec) == TAIL_TOOLS
    entry = entries(spec)["orchestrator"]
    assert entry["source"] == RESERVED_SOURCE and "plugin" not in entry
    assert entry["allow"] == TAIL_TOOLS
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
    assert on_start.orchestrator_allow(spec) == [*hive_allow, *TAIL_TOOLS]
    assert entries(spec)["orchestrator"]["allow"] == [*hive_allow, *TAIL_TOOLS]


def test_static_hive_has_no_scale_tool():
    spec = SessionSpec(replicas=2, static=True)
    assert on_start.orchestrator_allow(spec) == [*HIVE_TOOLS, *TAIL_TOOLS]


def test_hive_entry_and_allow():
    spec = SessionSpec(replicas=2, max_replicas=4, auto_compact_pct=70)
    assert on_start.orchestrator_allow(spec) == [*HIVE_TOOLS, "scale", *TAIL_TOOLS]
    entry = entries(spec)["orchestrator"]
    assert entry == {
        "name": "orchestrator",
        "source": RESERVED_SOURCE,
        "namespace": "orchestrator",
        "allow": [*HIVE_TOOLS, "scale", *TAIL_TOOLS],
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
    entry = next(p for p in cfg["plugins"] if p["name"] == "orchestrator")
    assert entry["source"] == RESERVED_SOURCE and "plugin" not in entry
    AnyMcpConfig.model_validate(cfg)


@pytest.mark.parametrize("spec", [SessionSpec(replicas=1), SessionSpec(replicas=2, max_replicas=4)])
def test_reserved_entry_is_plugins_repo_source(spec):
    """D7: the reserved entry is a source in the plugins repo at the pinned ref, never a plugin."""
    settings = make_settings(
        any_mcp_plugins_repo="https://git.example/acme/plugins", any_mcp_plugins_ref="v7.8.9"
    )
    cfg = written_config(on_start.render_on_start(spec, settings, "alpha"))
    entry = next(p for p in cfg["plugins"] if p["name"] == "orchestrator")
    assert entry["source"] == (
        "git::https://git.example/acme/plugins//orchestration/orchestrator?ref=v7.8.9"
    )
    assert set(entry) == {"name", "namespace", "source", "allow", "config"}
    assert entry["namespace"] == "orchestrator"
    assert entry["allow"] == on_start.orchestrator_allow(spec)
    assert entry["config"]["slug"] == "alpha"
    config = AnyMcpConfig.model_validate(cfg)
    plugin = next(p for p in config.plugins if p.name == "orchestrator")
    assert plugin.plugin is None and plugin.source == entry["source"]
    OrchestratorPlugin.Config.model_validate(plugin.config)


def test_any_mcp_sources_rendered():
    """D8: each source is `{name, source}`, after the base entries and before the reserved one."""
    sources = ["github.com/acme/tools", "git::https://git.example/x/mono.git//plugins/Lint?ref=v1"]
    user = {"plugins": [{"name": "gh", "plugin": "x:Y"}], "tools": {}}
    spec = SessionSpec(replicas=2, any_mcp_config=user, any_mcp_sources=sources)
    cfg = written_config(on_start.render_on_start(spec, make_settings(), "alpha"))
    assert cfg["plugins"][:3] == [
        {"name": "gh", "plugin": "x:Y"},
        {"name": "tools", "source": sources[0]},
        {"name": "lint", "source": sources[1]},
    ]
    assert [p["name"] for p in cfg["plugins"]] == ["gh", "tools", "lint", "orchestrator"]
    assert cfg["tools"] == {}
    AnyMcpConfig.model_validate(cfg)

    for settings, slug in ((make_settings(agent_api_url=None), "alpha"), (make_settings(), None)):
        cfg = written_config(on_start.render_on_start(spec, settings, slug))
        assert [p["name"] for p in cfg["plugins"]] == ["gh", "tools", "lint"]
        AnyMcpConfig.model_validate(cfg)

    bare = SessionSpec(replicas=1, any_mcp_sources=sources[:1])
    cfg = written_config(on_start.render_on_start(bare, make_settings(agent_api_url=None)))
    assert cfg == {"plugins": [{"name": "tools", "source": sources[0]}]}


async def test_allowed_tools_exist_in_plugin():
    """Every tool name rendered or named to agents is offered by the pinned plugin."""
    from any_mcp.testing import PluginHarness

    from agents_orchestrator.dispatcher.session_frame import frame
    from agents_orchestrator.reconciler.init import render_init_sh

    specs = (
        SessionSpec(replicas=1),
        SessionSpec(replicas=2, static=True),
        SessionSpec(replicas=2, max_replicas=3),
    )
    offered: set[str] = set()
    allowed: set[str] = set()
    for spec in specs:
        entry = entries(spec)["orchestrator"]
        config = {
            **entry["config"],
            "base_url": "http://orch:8080",
            "token": "t",
            "session": "1",
            "agent": "a0",
        }
        async with PluginHarness(OrchestratorPlugin, config=config) as h:
            # the harness prefixes the class name; the rendered namespace is "orchestrator"
            offered |= {t["name"].removeprefix("orchestratorplugin_") for t in await h.list_tools()}
        allowed |= set(entry["allow"])
    named = set(
        re.findall(
            r"orchestrator_[a-z_]+", render_init_sh("alpha", ["true"]) + frame("x", "alpha", 1)
        )
    )
    assert named == {"orchestrator_init_scripts_edit", "orchestrator_session_send"}
    assert allowed | {n.removeprefix("orchestrator_") for n in named} <= offered
