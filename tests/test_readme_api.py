"""README API coverage and the committed OpenAPI document (D2, D11)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from agent_orchestrator.api import openapi
from agent_orchestrator.api.app import create_app
from agent_orchestrator.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
STREAMS = (ROOT / "src" / "agent_orchestrator" / "api" / "streams.py").read_text(encoding="utf-8")


def section(title: str) -> str:
    """The body of the `## title` section (up to the next H2)."""
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _walk(router, prefix: str = ""):
    """(path, methods) of every route; FastAPI includes routers lazily, so descend into them."""
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route.methods
        elif hasattr(route, "original_router"):
            yield from _walk(route.original_router, prefix + route.include_context.prefix)


def app_routes() -> set[str]:
    app = create_app(Settings(database_url="postgresql://unused/db"), pool=None)
    out = set()
    for path, methods in _walk(app):
        if path.startswith("/api/v1"):
            out.update(f"{m} {path}" for m in methods - {"HEAD"})
    return out


def readme_routes() -> set[str]:
    found = re.findall(
        r"^### `(GET|POST|PATCH|PUT|DELETE) (/api/v1\S*)`\s*$", section("API"), re.MULTILINE
    )
    return {f"{m} {p}" for m, p in found}


def test_every_route_documented() -> None:
    expected = app_routes()
    assert len(expected) >= 16
    missing = expected - readme_routes()
    assert not missing, f"undocumented routes: {sorted(missing)}"


def test_no_stale_route_in_readme() -> None:
    stale = readme_routes() - app_routes()
    assert not stale, f"README documents routes the app lacks: {sorted(stale)}"


def test_openapi_json_matches_app() -> None:
    committed = (ROOT / "openapi.json").read_text(encoding="utf-8")
    assert committed == openapi.export(), (
        "run: uv run python -m agent_orchestrator.api.openapi > openapi.json"
    )
    assert committed.endswith("\n")


def test_sse_event_types_documented() -> None:
    emitted = set(re.findall(r'event="([a-z_]+)"', STREAMS))
    assert {
        "snapshot",
        "cluster_status",
        "agent_status",
        "message_status",
        "cluster_removed",
    } <= emitted
    events = section("API").split("### Events", 1)
    assert len(events) == 2, "README ## API has no '### Events' subsection"
    rows = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", events[1].split("\n### ")[0], re.MULTILINE))
    missing = emitted - rows
    assert not missing, f"undocumented SSE event names: {sorted(missing)}"


def test_readme_documents_allowlist_and_limits() -> None:
    api = section("API")
    for needle in (
        "forbidden",
        "cluster_limit",
        "ORCH_ALLOWED_CIDRS",
        "caps",
        "max_replicas",
        "additive",
    ):
        assert needle in api, needle


def test_architecture_documents_single_writer() -> None:
    arch = section("Architecture").lower()
    for needle in (
        "single-writer",
        "one writer per table",
        "cluster_changed",
        "agent_free",
        "polling",
    ):
        assert needle in arch, needle
    assert section("Data model").strip()


def test_frontend_schema_matches_openapi(tmp_path: Path) -> None:
    """frontend/src/api/schema.d.ts must be regenerated whenever openapi.json changes."""
    import json
    import shutil
    import subprocess

    spec = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    types = (ROOT / "frontend" / "src" / "api" / "schema.d.ts").read_text(encoding="utf-8")

    # Structural check (no node needed): every path, schema and property is in the types.
    missing = [p for p in spec["paths"] if f'"{p}"' not in types]
    for name, schema in spec.get("components", {}).get("schemas", {}).items():
        if not re.search(rf"^\s+{re.escape(name)}: ", types, re.MULTILINE):
            missing.append(f"schema {name}")
            continue
        for prop in schema.get("properties", {}):
            if not re.search(rf"^\s+{re.escape(prop)}\??: ", types, re.MULTILINE):
                missing.append(f"{name}.{prop}")
    assert not missing, f"schema.d.ts is stale (run `npm run gen:api`): {missing}"

    # Exact check: regenerate with the pinned generator and compare byte for byte.
    binary = ROOT / "frontend" / "node_modules" / ".bin" / "openapi-typescript"
    assert binary.exists() and shutil.which("node"), (
        "openapi-typescript is not installed: run `npm ci` in frontend/"
    )
    out = tmp_path / "schema.check.d.ts"
    subprocess.run([str(binary), str(ROOT / "openapi.json"), "-o", str(out)], check=True)
    assert out.read_text(encoding="utf-8") == types, (
        "schema.d.ts differs from generated output (run `npm run gen:api`)"
    )
