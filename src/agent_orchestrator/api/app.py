"""FastAPI app factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

from agent_orchestrator import __version__
from agent_orchestrator.api import clusters, errors, messages, presets, streams, workspace
from agent_orchestrator.api.allowlist import PortGate, SourceAllowlist
from agent_orchestrator.api.errors import error_response
from agent_orchestrator.api.listener import Listener
from agent_orchestrator.api.origin import OriginAllowlist
from agent_orchestrator.api.spa import SpaStaticFiles
from agent_orchestrator.db.pool import open_pool
from agent_orchestrator.settings import Settings


def create_app(
    settings: Settings,
    pool: AsyncConnectionPool | None = None,
    stream_poll_seconds: float = streams.DEFAULT_POLL_SECONDS,
) -> FastAPI:
    owns_pool = pool is None
    listener = Listener(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_pool:
            app.state.pool = await open_pool(settings.database_url)
        await listener.start()
        try:
            yield
        finally:
            await listener.stop()
            await workspace.close_client(app)
            if owns_pool:
                await app.state.pool.close()

    app = FastAPI(
        title="agent-orchestrator",
        version=__version__,
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.pool = pool
    app.state.listener = listener
    app.state.wrapper_http = None
    app.state.stream_poll_seconds = stream_poll_seconds
    errors.install(app)
    app.include_router(clusters.router)
    app.include_router(presets.router)
    app.include_router(messages.router)
    app.include_router(streams.router)
    app.include_router(workspace.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        try:
            async with app.state.pool.connection(timeout=3) as conn:
                await conn.execute("SELECT 1")
        except Exception as exc:
            return error_response(503, "internal", f"database unavailable: {type(exc).__name__}")
        return JSONResponse({"status": "ready"})

    if settings.static_dir and Path(settings.static_dir).is_dir():
        app.mount("/", SpaStaticFiles(directory=settings.static_dir, html=True), name="spa")

    app.add_middleware(OriginAllowlist, origins=settings.allowed_origins)
    app.add_middleware(
        SourceAllowlist, networks=settings.allowed_networks(), skip_port=settings.agent_port
    )
    app.add_middleware(PortGate, agent_port=settings.agent_port)
    return app
