"""The one seam through which the api calls wrapper pods (user decision N1, amends D5).

This is the only api module allowed to import `agent_orchestrator.wrapper` or
`agent_orchestrator.tokens` (tests/test_api_clusters.py pins it). The file and terminal proxy
routes resolve an agent with `resolve_agent`, validate a terminal id with
`validate_terminal_id`, and call the wrapper with `forward`. There is no Kubernetes import:
agents come from the database and the token is derived from the master key.

Token hygiene: the derived bearer lives in `WrapperTarget.token` (excluded from repr) and
travels only as the `Authorization` header set by `WrapperClient`. It is never put in a URL,
a query string, a response, an error message or a log record.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg
from fastapi import FastAPI

from agent_orchestrator.api.errors import ApiError, not_found
from agent_orchestrator.db import agents as agents_repo
from agent_orchestrator.db import clusters as clusters_repo
from agent_orchestrator.settings import Settings, SettingsError
from agent_orchestrator.tokens import tokens_for
from agent_orchestrator.wrapper import WrapperClient, WrapperError, WrapperUnreachable

log = logging.getLogger(__name__)

# the wrapper terminal registry's id format; the one place the api pins it
TERMINAL_ID_RE = re.compile(r"^term_[0-9a-f]{12}$")

_DEAD = "dead"
_MASK = "***"


def validate_terminal_id(value: str) -> str:
    """The id unchanged when it is a wrapper terminal id, else 422 `invalid_request`.

    Called by every terminal route and the WebSocket relay before any upstream request, so a
    crafted id such as `..` can never steer the derived bearer to another upstream path.
    `fullmatch` semantics: a trailing newline does not slip past `$`.
    """
    if not isinstance(value, str) or not TERMINAL_ID_RE.fullmatch(value):
        raise ApiError(422, "invalid_request", "invalid terminal id")
    return value


@dataclass(frozen=True)
class WrapperTarget:
    """One reachable wrapper pod and the bearer for its cluster."""

    agent_id: uuid.UUID
    cluster_id: uuid.UUID
    pod_name: str
    url: str
    session_id: str
    token: str = field(repr=False)


def _usable(agent: agents_repo.Agent, state: str | None) -> bool:
    return (
        agent.ready
        and agent.gone_at is None
        and bool(agent.url)
        and bool(agent.session_id)
        and state != _DEAD
    )


async def resolve_agent(
    conn: psycopg.AsyncConnection,
    settings: Settings,
    *,
    cluster_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> WrapperTarget:
    """The wrapper pod to call.

    With `agent_id` that agent (404 `not_found` when unknown or when `cluster_id` is given and
    differs); with only `cluster_id` the first agent by ordinal that is usable. Usable is the
    dispatcher's notion (db/queue.py `_PICK`) minus idleness, since files and terminals work
    on a busy pod: the pod is ready, the agent is not gone, it has a url and a wrapper
    session, its stored state is not `dead`, and its cluster is `desired = 'active'` (a
    discarding cluster is being torn down). None usable, or the named agent not usable, is 409
    `agent_not_ready`; an unknown cluster is 404; an unset master key is 503 `unavailable`.
    """
    if agent_id is None and cluster_id is None:
        raise ApiError(422, "invalid_request", "an agent or a cluster is required")
    if agent_id is not None:
        agent = await agents_repo.get_agent(conn, agent_id)
        if agent is None or (cluster_id is not None and agent.cluster_id != cluster_id):
            raise not_found("agent")
        cluster_id = agent.cluster_id
    assert cluster_id is not None
    cluster = await clusters_repo.get(conn, cluster_id)
    if cluster is None:
        raise not_found("cluster")
    states = await agents_repo.list_states(conn, cluster_id)
    if agent_id is not None:
        pool = [agent]
    else:
        pool = await agents_repo.list_agents(conn, cluster_id)
    candidates = [a for a in pool if _usable(a, states.get(a.id))]
    if cluster.desired != "active" or not candidates:
        raise ApiError(409, "agent_not_ready", "no agent of the cluster is ready")
    chosen = candidates[0]
    try:
        token = tokens_for(settings, cluster_id).auth
    except SettingsError:
        raise ApiError(503, "unavailable", "wrapper access is not configured") from None
    assert chosen.url is not None and chosen.session_id is not None
    return WrapperTarget(
        agent_id=chosen.id,
        cluster_id=cluster_id,
        pod_name=chosen.pod_name,
        url=chosen.url,
        session_id=chosen.session_id,
        token=token,
    )


# -------------------------------------------------------------------------- shared client


def new_client() -> httpx.AsyncClient:
    # pod addresses are cluster-internal: never route them through an environment proxy
    return httpx.AsyncClient(trust_env=False)


def get_client(app: FastAPI) -> httpx.AsyncClient:
    """The app's shared wrapper client, created on first use and kept on `app.state`.

    Created lazily so an app driven through `httpx.ASGITransport` (which never runs the
    lifespan) works; `close_client` runs in the lifespan shutdown. Tests may preset
    `app.state.wrapper_http` with their own client.
    """
    client = getattr(app.state, "wrapper_http", None)
    if client is None:
        client = app.state.wrapper_http = new_client()
    return client


async def close_client(app: FastAPI) -> None:
    client = getattr(app.state, "wrapper_http", None)
    if client is not None:
        app.state.wrapper_http = None
        await client.aclose()


# -------------------------------------------------------------------------------- forward


def _scrub(text: str, token: str) -> str:
    return text.replace(token, _MASK) if token else text


async def forward(
    app: FastAPI,
    target: WrapperTarget,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    timeout: float | None = None,
) -> Any:
    """Call the wrapper `/v1{path}` of `target` and map the outcome.

    2xx returns the wrapper's JSON; a wrapper 4xx is re-raised as `ApiError` with the wrapper's
    status and code; a wrapper 5xx is 502 `wrapper_error` (its body is not relayed); a
    transport failure is 502 `wrapper_unreachable`.
    """
    client = WrapperClient(target.url, target.token, http=get_client(app))
    extra = {} if timeout is None else {"timeout": timeout}
    log.debug(
        "wrapper request %s %s agent=%s pod=%s", method, path, target.agent_id, target.pod_name
    )
    try:
        status, body = await client.request_json(method, path, params=params, json=json, **extra)
    except WrapperError as exc:
        log.debug("wrapper answered %s %s on %s %s", exc.status, exc.code, method, path)
        if exc.status >= 500 or exc.status < 400:
            raise ApiError(
                502, "wrapper_error", f"wrapper answered {exc.status} on {target.pod_name}"
            ) from None
        raise ApiError(
            exc.status, _scrub(exc.code, target.token), _scrub(exc.message, target.token)
        ) from None
    except WrapperUnreachable as exc:
        log.debug("wrapper unreachable on %s %s: %s", method, path, type(exc.__cause__).__name__)
        raise ApiError(
            502, "wrapper_unreachable", f"wrapper of {target.pod_name} is unreachable"
        ) from None
    log.debug("wrapper answered %s on %s %s", status, method, path)
    return body
