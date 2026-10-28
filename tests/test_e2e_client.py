"""The e2e api client never reuses a connection the api may be closing.

The kind e2e reaches the api through `kubectl port-forward` with one module-scoped httpx
client. uvicorn closes an idle keep-alive connection after `timeout_keep_alive` seconds (5 by
default); httpx also keeps an idle connection for 5 seconds by default. A test that is idle
for about that long (kubectl calls between two api requests) can send its next request on a
connection the server closes in the same instant, which fails with `Server disconnected
without sending a response` (CI run 40565058706, test_in_session_pod_refused_by_allowlist).
The client therefore drops an idle connection well before the server does.
"""

from __future__ import annotations

from e2e_kind_loader import e2e_kind

from agents_orchestrator import cli
from agents_orchestrator.settings import Settings

MARGIN_SECONDS = 2.0


def test_api_client_drops_idle_connections_before_the_server_does() -> None:
    server = cli.build_uvicorn_config(Settings(database_url="postgresql://u:p@localhost/db"))
    client = e2e_kind().api_client("http://127.0.0.1:18080")
    try:
        pool = client._transport._pool  # httpx exposes the keep-alive window only on the pool
        assert pool._keepalive_expiry is not None
        assert pool._keepalive_expiry <= server.timeout_keep_alive - MARGIN_SECONDS
        assert str(client.base_url) == "http://127.0.0.1:18080/api/v1/"
        assert client.timeout.read == 30
    finally:
        client.close()
