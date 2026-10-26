"""The per-session onStart script: installs any-mcp once per ref and writes its config (D16)."""

from __future__ import annotations

import json
import shlex
from typing import Any

from agents_orchestrator.settings import Settings
from agents_orchestrator.spec import SessionSpec

DELIMITER = "ANY_MCP_CONFIG_EOF"
ROOT = "/opt/any-mcp"
CONFIG_PATH = f"{ROOT}/etc/any-mcp.yaml"


PLUGIN_NAME = "orchestrator"
PLUGIN_CLASS = "any_mcp.orchestrator:OrchestratorPlugin"
SCALE_TOOL = "scale"
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


def orchestrator_allow(spec: SessionSpec) -> list[str]:
    """The orchestrator tools this session's agents may call.

    Every session gets the session tools (D11) then the init-scripts tools, alone sessions
    included; the agent API decides what each caller may list, send and edit, so the list never
    depends on `session_messaging`.
    """
    if not spec.hive:
        return [*SESSION_TOOLS, *INIT_SCRIPTS_TOOLS]
    scale = [SCALE_TOOL] if spec.scalable else []
    return [*HIVE_TOOLS, *scale, *SESSION_TOOLS, *INIT_SCRIPTS_TOOLS]


def _orchestrator_entry(
    spec: SessionSpec, settings: Settings, slug: str | None
) -> dict[str, Any] | None:
    if not settings.agent_api_url or not slug:
        return None
    return {
        "name": PLUGIN_NAME,
        "plugin": PLUGIN_CLASS,
        "namespace": PLUGIN_NAME,
        "allow": orchestrator_allow(spec),
        "config": {
            "base_url": "${ORCH_AGENT_URL}",
            "token": "${ORCH_AGENT_TOKEN}",
            "session": "${ORCH_SESSION_ID}",
            "agent": "${POD_NAME}",
            "slug": slug,
            "max_replicas": spec.max_replicas or spec.replicas,
            "static": spec.static,
            "hive": spec.hive,
            "auto_compact_pct": spec.auto_compact_pct,
        },
    }


def any_mcp_config(
    spec: SessionSpec, settings: Settings, slug: str | None = None
) -> dict[str, Any]:
    base = (
        spec.any_mcp_config
        if spec.any_mcp_config is not None
        else settings.default_any_mcp_config()
    )
    entry = _orchestrator_entry(spec, settings, slug)
    if entry is None:
        return base
    kept = [p for p in base.get("plugins", []) if p.get("name") != PLUGIN_NAME]
    return {**base, "plugins": [*kept, entry]}


def render_on_start(spec: SessionSpec, settings: Settings, slug: str | None = None) -> str:
    body = json.dumps(any_mcp_config(spec, settings, slug), indent=2, sort_keys=True)
    if DELIMITER in body.splitlines():
        raise ValueError(f"any-mcp config contains the heredoc delimiter line {DELIMITER}")
    ref = shlex.quote(settings.any_mcp_ref)
    requirement = shlex.quote(f"any-mcp @ git+{settings.any_mcp_repo}@{settings.any_mcp_ref}")
    return f"""#!/bin/sh
set -eu
REF={ref}
if ! grep -qxF -- "$REF" {ROOT}/.ref 2>/dev/null; then
  rm -rf {ROOT}/* {ROOT}/.ref
  python3 -m venv {ROOT}
  {ROOT}/bin/pip install --no-cache-dir {requirement}
  printf '%s\\n' "$REF" > {ROOT}/.ref
fi
mkdir -p {ROOT}/etc
cat > {CONFIG_PATH} <<'{DELIMITER}'
{body}
{DELIMITER}
{ROOT}/bin/any-mcp --help >/dev/null
"""
