"""The per-cluster onStart script: installs any-mcp once per ref and writes its config (D16)."""

from __future__ import annotations

import json
import shlex
from typing import Any

from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec

DELIMITER = "ANY_MCP_CONFIG_EOF"
ROOT = "/opt/any-mcp"
CONFIG_PATH = f"{ROOT}/etc/any-mcp.yaml"


PLUGIN_NAME = "orchestrator"
PLUGIN_CLASS = "any_mcp.orchestrator:OrchestratorPlugin"
HIVE_TOOLS = [
    "me",
    "leadership_handover",
    "lock_claim",
    "lock_release",
    "lock_list",
    "lock_reassign",
]


def orchestrator_allow(spec: ClusterSpec) -> list[str]:
    """The orchestrator tools this cluster's agents may call; empty means no plugin entry."""
    return list(HIVE_TOOLS) if spec.hive else []


def _orchestrator_entry(
    spec: ClusterSpec, settings: Settings, slug: str | None
) -> dict[str, Any] | None:
    allow = orchestrator_allow(spec)
    if not allow or not settings.agent_api_url or not slug:
        return None
    return {
        "name": PLUGIN_NAME,
        "plugin": PLUGIN_CLASS,
        "namespace": PLUGIN_NAME,
        "allow": allow,
        "config": {
            "base_url": "${ORCH_AGENT_URL}",
            "token": "${ORCH_AGENT_TOKEN}",
            "cluster": "${ORCH_CLUSTER_ID}",
            "agent": "${POD_NAME}",
            "slug": slug,
            "max_replicas": spec.max_replicas or spec.replicas,
            "static": spec.static,
            "hive": spec.hive,
            "auto_compact_pct": spec.auto_compact_pct,
        },
    }


def any_mcp_config(
    spec: ClusterSpec, settings: Settings, slug: str | None = None
) -> dict[str, Any]:
    base = (
        spec.any_mcp_config
        if spec.any_mcp_config is not None
        else settings.default_any_mcp_config()
    )
    entry = _orchestrator_entry(spec, settings, slug)
    if entry is None:
        return base
    return {**base, "plugins": [*base.get("plugins", []), entry]}


def render_on_start(spec: ClusterSpec, settings: Settings, slug: str | None = None) -> str:
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
