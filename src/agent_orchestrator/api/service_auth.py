"""Service-to-service bearer tokens on the dedicated service port (D10).

In-cluster trigger services (agents-webhook, agents-schedule) call the orchestrator on
ORCH_SERVICE_PORT with `Authorization: Bearer <token>`. Only the sha256 of each token is
configured (ORCH_SERVICE_TOKENS), compared in constant time against every entry. The port
serves a fixed list of method and path pairs and never a WebSocket; the token value never
reaches a log line or an error body.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Mapping

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_orchestrator.api.allowlist import EXEMPT_PATHS, _port
from agent_orchestrator.api.errors import error_response

log = logging.getLogger(__name__)

_ID = r"[^/]+"

ALLOWED_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(f"^{path.replace('{id}', _ID)}$"))
    for method, path in (
        ("GET", "/api/v1/meta"),
        ("GET", "/api/v1/presets"),
        ("GET", "/api/v1/presets/{id}"),
        ("GET", "/api/v1/clusters"),
        ("POST", "/api/v1/clusters"),
        ("GET", "/api/v1/clusters/{id}"),
        ("GET", "/api/v1/clusters/{id}/messages"),
        ("POST", "/api/v1/clusters/{id}/messages"),
        ("GET", "/api/v1/messages/{id}"),
    )
)


def route_allowed(method: str, path: str) -> bool:
    return any(m == method and p.match(path) for m, p in ALLOWED_ROUTES)


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            token = token.strip()
            return token if scheme.lower() == "bearer" and token else None
    return None


class ServiceGate:
    """Bearer service-token gate, active only for connections accepted on `service_port`."""

    def __init__(self, app: ASGIApp, service_port: int, token_hashes: Mapping[str, str]):
        self.app = app
        self.service_port = service_port
        self.hashes = [(name, digest.encode()) for name, digest in token_hashes.items()]

    def authenticate(self, token: str) -> str | None:
        """The service name whose hash matches; every entry is compared, no early exit."""
        presented = hashlib.sha256(token.encode()).hexdigest().encode()
        found = None
        for name, digest in self.hashes:
            if hmac.compare_digest(presented, digest) and found is None:
                found = name
        return found

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or _port(scope) != self.service_port:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        method, path = scope["method"], scope.get("path", "")
        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        token = _bearer(scope)
        service = self.authenticate(token) if token is not None else None
        if service is None:
            log.info("service request service=- method=%s path=%s status=401", method, path)
            response = error_response(
                401,
                "unauthorized",
                "a valid service token is required",
                {"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        if not route_allowed(method, path):
            log.info(
                "service request service=%s method=%s path=%s status=403", service, method, path
            )
            response = error_response(403, "forbidden", "route is not served on the service port")
            await response(scope, receive, send)
            return

        async def logged_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                log.info(
                    "service request service=%s method=%s path=%s status=%s",
                    service,
                    method,
                    path,
                    message["status"],
                )
            await send(message)

        await self.app(scope, receive, logged_send)
