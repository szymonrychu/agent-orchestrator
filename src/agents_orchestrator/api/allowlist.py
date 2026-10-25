"""Source allowlist on the direct socket peer (D22).

Forwarded, X-Forwarded-For and X-Real-IP are never read: ingress-nginx is hostNetwork, so
ingress traffic arrives from node IPs, and pod-CIDR callers must be refused.
"""

from __future__ import annotations

import ipaddress

from starlette.types import ASGIApp, Receive, Scope, Send

from agents_orchestrator.api.errors import error_response

EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})


AGENT_PREFIX = "/agent/v1"


def is_agent_path(path: str) -> bool:
    return path == AGENT_PREFIX or path.startswith(AGENT_PREFIX + "/")


def _port(scope: Scope) -> int | None:
    server = scope.get("server")
    return server[1] if server else None


class PortGate:
    """Splits the listening sockets of `orch api` by the accepting port.

    The agent port serves only /agent/v1/*, /healthz and /readyz; the main port never serves
    /agent/v1/*. The service port never serves /agent/v1/* nor a WebSocket; `ServiceGate`
    narrows it further to its route list. Everything else is 404 not_found.
    """

    def __init__(self, app: ASGIApp, agent_port: int, service_port: int | None = None):
        self.app = app
        self.agent_port = agent_port
        self.service_port = service_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        port = _port(scope)
        if port == self.agent_port:
            served = is_agent_path(path) or path in EXEMPT_PATHS
        elif port is not None and port == self.service_port:
            served = scope["type"] == "http" and not is_agent_path(path)
        else:
            served = not is_agent_path(path)
        if served:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await error_response(404, "not_found", "not found")(scope, receive, send)


class SourceAllowlist:
    """Refuses peers outside the allowlist on the main port; the agent and service ports
    authenticate on their own and are skipped."""

    def __init__(
        self,
        app: ASGIApp,
        networks: list,
        skip_port: int | None = None,
        service_port: int | None = None,
    ):
        self.app = app
        self.networks = list(networks)
        self.skip_ports = frozenset(p for p in (skip_port, service_port) if p is not None)

    def allowed(self, scope: Scope) -> bool:
        client = scope.get("client")
        if not client:
            return False
        try:
            addr = ipaddress.ip_address(client[0])
        except ValueError:
            return False
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return any(addr.version == n.version and addr in n for n in self.networks)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if _port(scope) in self.skip_ports:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("path") in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if self.allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = error_response(403, "forbidden", "source address is not allowed")
        await response(scope, receive, send)
