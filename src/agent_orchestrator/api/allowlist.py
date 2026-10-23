"""Source allowlist on the direct socket peer (D22).

Forwarded, X-Forwarded-For and X-Real-IP are never read: ingress-nginx is hostNetwork, so
ingress traffic arrives from node IPs, and pod-CIDR callers must be refused.
"""

from __future__ import annotations

import ipaddress

from starlette.types import ASGIApp, Receive, Scope, Send

from agent_orchestrator.api.errors import error_response

EXEMPT_PATHS = frozenset({"/healthz", "/readyz"})


class SourceAllowlist:
    def __init__(self, app: ASGIApp, networks: list):
        self.app = app
        self.networks = list(networks)

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
