"""The per-cluster onStart script: installs any-mcp once per ref and writes its config (D16)."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import yaml

from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec

DELIMITER = "ANY_MCP_CONFIG_EOF"
ROOT = "/opt/any-mcp"
CONFIG_PATH = f"{ROOT}/etc/any-mcp.yaml"


def any_mcp_config(spec: ClusterSpec, settings: Settings) -> dict[str, Any]:
    if spec.any_mcp_config is not None:
        return spec.any_mcp_config
    if settings.any_mcp_config_file:
        loaded = yaml.safe_load(Path(settings.any_mcp_config_file).read_text())
        return loaded or {}
    return {}


def render_on_start(spec: ClusterSpec, settings: Settings) -> str:
    body = json.dumps(any_mcp_config(spec, settings), indent=2, sort_keys=True)
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
