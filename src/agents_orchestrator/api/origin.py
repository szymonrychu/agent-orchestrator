"""Origin allowlist (D5).

Browsers apply no CORS to WebSocket upgrades, so any web page can open a socket to a LAN
orchestrator. Every upgrade carrying an Origin header must name a configured origin; a missing
Origin (a non-browser client) passes, `null` never does, and an empty list refuses every
Origin-bearing upgrade. The same rule guards the mutating terminal HTTP routes.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_orchestrator.api.errors import ApiError
from agents_orchestrator.settings import normalise_origin


def origin_allowed(origin: str | None, allowed: Iterable[str]) -> bool:
    if origin is None:
        return True
    canonical = normalise_origin(origin)
    return canonical is not None and canonical in set(allowed)


def _origin_header(headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == b"origin":
            return value.decode("latin-1")
    return None


class OriginAllowlist:
    """Pure ASGI: closes a websocket 1008 before accept when its Origin is not allowed."""

    def __init__(self, app: ASGIApp, origins: Iterable[str]):
        self.app = app
        self.origins = frozenset(origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket" and not origin_allowed(
            _origin_header(scope.get("headers", [])), self.origins
        ):
            await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


async def require_allowed_origin(request: Request) -> None:
    """FastAPI dependency: the same rule on an HTTP request, 403 forbidden_origin."""
    allowed = request.app.state.settings.allowed_origins
    if not origin_allowed(request.headers.get("origin"), allowed):
        raise ApiError(403, "forbidden_origin", "origin is not allowed")
