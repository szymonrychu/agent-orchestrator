"""FastAPI app factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

from agent_orchestrator import __version__
from agent_orchestrator.api import clusters, errors
from agent_orchestrator.api.allowlist import SourceAllowlist
from agent_orchestrator.api.errors import error_response
from agent_orchestrator.db.pool import open_pool
from agent_orchestrator.settings import Settings


def create_app(settings: Settings, pool: AsyncConnectionPool | None = None) -> FastAPI:
    owns_pool = pool is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_pool:
            app.state.pool = await open_pool(settings.database_url)
        try:
            yield
        finally:
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
    errors.install(app)
    app.include_router(clusters.router)

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

    app.add_middleware(SourceAllowlist, networks=settings.allowed_networks())
    return app
