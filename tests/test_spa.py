"""Retirement of the SPA mount (D9): the api is API-only."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from test_api_clusters import client_for, make_settings

from agent_orchestrator.api.app import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_spa_module_untracked():
    out = subprocess.run(
        ["git", "ls-files", "src/agent_orchestrator/api/spa.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == ""


@pytest.mark.parametrize("path", ["/", "/index.html", "/clusters/x"])
async def test_root_is_json_404(path):
    async with client_for(create_app(make_settings())) as c:
        r = await c.get(path)
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["error"]["code"] == "not_found"


async def test_patch_cluster_still_405():
    async with client_for(create_app(make_settings())) as c:
        r = await c.patch("/api/v1/clusters/6f1c2f0e-0000-4000-8000-000000000001", json={})
        assert r.status_code == 405
