"""Async client for claude-code-wrapper's /v1 API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from agent_orchestrator.wrapper.models import (
    Ambiguous,
    Busy,
    ConfigError,
    Envelope,
    Gone,
    PeersView,
    Rejected,
    Snapshot,
    SubmitOutcome,
    TurnAccepted,
    TurnRecord,
    TurnSummary,
    Unavailable,
    WrapperError,
    WrapperUnreachable,
)
from agent_orchestrator.wrapper.sse import SSEParser

DEFAULT_TIMEOUT = 30.0


def _error_parts(resp: httpx.Response) -> tuple[str, str]:
    try:
        err = resp.json()["error"]
        return str(err["code"]), str(err.get("message", ""))
    except (ValueError, KeyError, TypeError):
        return f"http_{resp.status_code}", resp.text[:200]


def _raise_for_error(resp: httpx.Response) -> None:
    if resp.status_code >= 300:
        code, message = _error_parts(resp)
        raise WrapperError(resp.status_code, code, message)


class WrapperClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        http: httpx.AsyncClient | None = None,
        send_timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.send_timeout = send_timeout
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None
        self._headers = {"Authorization": f"Bearer {token}"}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/v1{path}"

    async def _request(
        self, method: str, path: str, *, json: Any = None, timeout: float = DEFAULT_TIMEOUT
    ) -> httpx.Response:
        try:
            resp = await self._http.request(
                method, self._url(path), json=json, headers=self._headers, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise WrapperUnreachable(f"{type(exc).__name__}: {exc}") from exc
        _raise_for_error(resp)
        return resp

    # sessions

    async def list_sessions(self) -> list[Snapshot]:
        resp = await self._request("GET", "/sessions")
        return [Snapshot.model_validate(s) for s in resp.json()["sessions"]]

    async def create_session(
        self, name: str, workdir: str, model: str | None = None, effort: str | None = None
    ) -> Snapshot:
        body = {"name": name, "workdir": workdir, "model": model, "effort": effort}
        body = {k: v for k, v in body.items() if v is not None}
        # creation blocks until claude is ready, so it gets the long default
        resp = await self._request("POST", "/sessions", json=body, timeout=120.0)
        return Snapshot.model_validate(resp.json())

    async def get_session(self, session_id: str) -> Snapshot:
        return Snapshot.model_validate(
            (await self._request("GET", f"/sessions/{session_id}")).json()
        )

    async def resume_session(self, session_id: str) -> Snapshot:
        resp = await self._request("POST", f"/sessions/{session_id}/resume", timeout=120.0)
        return Snapshot.model_validate(resp.json())

    # turns

    async def submit_turn(
        self, session_id: str, text: str, metadata: dict[str, Any] | None = None
    ) -> SubmitOutcome:
        body: dict[str, Any] = {"text": text}
        if metadata is not None:
            body["metadata"] = metadata
        try:
            resp = await self._http.post(
                self._url(f"/sessions/{session_id}/turns"),
                json=body,
                headers=self._headers,
                timeout=self.send_timeout,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            return Unavailable(f"{type(exc).__name__}: {exc}")
        except httpx.HTTPError as exc:  # read timeout, reset, write failure: may be delivered
            return Ambiguous(f"{type(exc).__name__}: {exc}")
        return self._map_submit(resp)

    @staticmethod
    def _map_submit(resp: httpx.Response) -> SubmitOutcome:
        status = resp.status_code
        if 200 <= status < 300:
            try:
                return TurnAccepted(str(resp.json()["turn_id"]))
            except (ValueError, KeyError, TypeError):
                return Ambiguous(f"{status} without a turn_id")
        code, message = _error_parts(resp)
        reason = f"{status} {code}: {message}"
        if status == 409:
            return Busy()
        if status in (404, 410):
            return Gone(reason)
        if status in (401, 403):
            return ConfigError(reason)
        if status in (500, 502):
            # the wrapper may have recorded the turn before failing: never resend blindly
            return Ambiguous(reason)
        if status >= 500:
            return Unavailable(reason)
        return Rejected(reason)

    async def list_turns(self, session_id: str) -> list[TurnSummary]:
        resp = await self._request("GET", f"/sessions/{session_id}/turns")
        return [TurnSummary.model_validate(t) for t in resp.json()["turns"]]

    async def get_turn(self, session_id: str, turn_id: str) -> TurnRecord:
        resp = await self._request("GET", f"/sessions/{session_id}/turns/{turn_id}")
        return TurnRecord.model_validate(resp.json())

    # control

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        return (await self._request("POST", f"/sessions/{session_id}/interrupt")).json()

    async def command(self, session_id: str, name: str) -> dict[str, Any]:
        resp = await self._request("POST", f"/sessions/{session_id}/commands", json={"name": name})
        return resp.json()

    async def peers(self) -> PeersView:
        return PeersView.model_validate((await self._request("GET", "/peers")).json())

    # events

    async def events(
        self, session_id: str, last_event_id: int | str | None = None
    ) -> AsyncIterator[Envelope]:
        headers = {**self._headers, "Accept": "text/event-stream"}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        parser = SSEParser()
        try:
            async with self._http.stream(
                "GET",
                self._url(f"/sessions/{session_id}/events"),
                headers=headers,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT, read=None),
            ) as resp:
                if resp.status_code >= 300:
                    await resp.aread()
                    _raise_for_error(resp)
                async for chunk in resp.aiter_bytes():
                    for ev in parser.feed(chunk):
                        yield Envelope.model_validate_json(ev.data)
        except httpx.HTTPError as exc:
            raise WrapperUnreachable(f"{type(exc).__name__}: {exc}") from exc
