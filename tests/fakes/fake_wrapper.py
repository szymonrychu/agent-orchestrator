"""In-process fake of the claude-code-wrapper /v1 API, served by uvicorn on 127.0.0.1.

It listens on a real socket so SSE, timeouts and connection resets behave as over the network.
tests/test_fake_wrapper_contract.py runs one suite against this fake and the real wrapper image,
which keeps it honest. Turns stay `running` until a test calls `complete_turn`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import stat
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from claude_code_wrapper.events import EVENT_TYPES
from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import HTTPConnection

MAX_SESSIONS = 4
REPLAY = 100
MAX_PAGE = 1000
BINARY_SNIFF_BYTES = 8192
RESUMABLE = ("stopped", "failed", "dead")
_CLOSE = object()

log = logging.getLogger(__name__)

# the wrapper terminal protocol (wrapper README, design D2/D3)
TERMINAL_ID_RE = re.compile(r"^term_[0-9a-f]{12}$")
TERMINALS_MAX = 8
TERMINAL_SCROLLBACK_BYTES = 262144
TERMINAL_MAX_FRAME_BYTES = 65536
TERMINAL_SIZE = range(1, 1001)
ACTIVE_STATES = ("ready", "busy")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _iso(timestamp: float) -> str:
    stamp = datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z")


def _confine(workdir: Path, rel: str) -> Path:
    """The wrapper's confinement rule: relative, no NUL, real path inside the real workdir."""
    if "\x00" in rel or os.path.isabs(rel):
        raise _Err(400, "invalid_path", "path must be relative and free of NUL bytes")
    root = Path(os.path.realpath(workdir))
    resolved = Path(os.path.realpath(root / rel))
    if resolved != root and not resolved.is_relative_to(root):
        raise _Err(400, "invalid_path", "path escapes the workdir")
    return resolved


def _entry_type(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


class _Err(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status, self.code, self.message = status, code, message or code


@dataclass
class _Turn:
    turn_id: str
    text: str
    metadata: dict[str, Any]
    started_at: str
    state: str = "running"
    final_text: str | None = None
    completed_at: str | None = None

    def record(self, session_id: str) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "session_id": session_id,
            "origin": "client",
            "state": self.state,
            "text": self.text,
            "final_text": self.final_text,
            "stop_reason": "end_turn" if self.state == "completed" else None,
            "error": None,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "annotations": {},
            "metadata": self.metadata,
        }

    def summary(self) -> dict[str, Any]:
        rec = self.record("")
        return {
            k: rec[k]
            for k in ("turn_id", "origin", "state", "started_at", "completed_at", "stop_reason")
        }


@dataclass
class _Session:
    session_id: str
    name: str | None
    workdir: str
    model: str | None
    state: str = "creating"
    has_transcript: bool = False
    idle_since: str | None = None
    context: dict[str, Any] | None = None
    current_turn_id: str | None = None
    turns: dict[str, _Turn] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1
    turns_completed: int = 0
    created_at: str = field(default_factory=_now)
    subscribers: set[asyncio.Queue[Any]] = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "state": self.state,
            "step": self.state,
            "error": None,
            "workdir": self.workdir,
            "model": self.model,
            "effort": None,
            "created_at": self.created_at,
            "updated_at": _now(),
            "last_activity_at": _now(),
            "idle_since": self.idle_since if self.state == "ready" else None,
            "turns_completed": self.turns_completed,
            "outstanding_subagents": 0,
            "current_turn_id": self.current_turn_id,
            "stall_suspected_since": None,
            "restarts": 0,
            "context": self.context,
        }


@dataclass
class _Terminal:
    terminal_id: str
    session_id: str
    rows: int
    cols: int
    created_at: str = field(default_factory=_now)
    alive: bool = True
    exit_code: int | None = None
    scrollback: bytearray = field(default_factory=bytearray)
    clients: set[asyncio.Queue[Any]] = field(default_factory=set)

    def record(self) -> dict[str, Any]:
        return {
            "terminal_id": self.terminal_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "alive": self.alive,
            "exit_code": self.exit_code,
            "attached": bool(self.clients),
            "rows": self.rows,
            "cols": self.cols,
        }

    def output(self, data: bytes) -> None:
        """Append to the replay buffer (capped) and fan the bytes out to every client."""
        self.scrollback += data
        del self.scrollback[:-TERMINAL_SCROLLBACK_BYTES]
        for queue in list(self.clients):
            queue.put_nowait(data)

    def finish(self, code: int) -> None:
        """The shell exited: every client gets the exit frame, then a clean close."""
        self.alive, self.exit_code = False, code
        for queue in list(self.clients):
            queue.put_nowait({"type": "exit", "code": code})
            queue.put_nowait(1000)


class FakeWrapper:
    def __init__(self, token: str = "test-token", ping_interval: float = 15.0) -> None:
        self.token = token
        self.ping_interval = ping_interval
        self.sessions: dict[str, _Session] = {}
        self.peers: dict[str, Any] = {"enabled": False}
        self.received_turns = 0
        self.turn_texts: list[str] = []
        self.turn_metadata: list[dict[str, Any]] = []
        self.last_turn_id = ""
        self._ids = 0
        self._booting = False
        self._status: int | None = None
        self._status_after: int | None = None
        self._delay = 0.0
        self._reset_after = False
        self._reset_before = False
        self._unreachable = False
        self._uv: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._port = 0
        # file API: every session gets a temp workdir under this root (removed with the fake)
        self._tmp = tempfile.TemporaryDirectory(prefix="fake-wrapper-")
        self.files_max_entries = 2000
        self.file_preview_max_bytes = 1048576
        # terminal API: per-process registry, as in the wrapper
        self.terminals: dict[str, _Terminal] = {}
        self.terminals_max = TERMINALS_MAX
        self.requests: list[tuple[str, str]] = []  # (method, path) of every authenticated call
        self.app = self._build_app()

    # ------------------------------------------------------------------ lifecycle

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @staticmethod
    def _bind(port: int = 0) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        sock.setblocking(False)
        return sock

    async def start(self) -> None:
        sock = self._bind()
        self._port = sock.getsockname()[1]
        config = uvicorn.Config(
            self.app,
            log_level="warning",
            lifespan="off",
            timeout_graceful_shutdown=1,
            ws="websockets-sansio",
        )
        self._uv = uvicorn.Server(config)
        self._task = asyncio.create_task(self._uv.serve(sockets=[sock]))
        while not self._uv.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self._uv is None or self._task is None:
            return
        self.drop_streams()
        self._uv.should_exit = True
        await asyncio.wait_for(self._task, 10)
        self._uv = self._task = None

    async def __aenter__(self) -> FakeWrapper:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------ test controls

    def workdir(self, session_id: str | None = None) -> Path:
        """The real directory of a session workdir (default: the newest session)."""
        return Path(self._session(session_id).workdir)

    def terminal(self, terminal_id: str | None = None) -> _Terminal:
        """A terminal by id (default: the newest one)."""
        if terminal_id is not None:
            return self.terminals[terminal_id]
        return next(reversed(self.terminals.values()))

    def terminal_output(self, data: bytes, terminal_id: str | None = None) -> None:
        """Make the shell print `data`: buffered for replay and sent to every attached client."""
        self.terminal(terminal_id).output(data)

    def exit_terminal(self, terminal_id: str | None = None, code: int = 0) -> None:
        """The shell exits with `code`; the terminal stays listed, not alive, until deleted."""
        self.terminal(terminal_id).finish(code)

    def _session(self, session_id: str | None = None) -> _Session:
        if session_id is not None:
            return self.sessions[session_id]
        return next(reversed(self.sessions.values()))

    def emit(self, type: str, data: dict[str, Any] | None = None, session_id: str | None = None):
        """Append an event to the session log (default: the newest session) and fan it out."""
        ses = self._session(session_id)
        env = {
            "seq": ses.next_seq,
            "ts": _now(),
            "session_id": ses.session_id,
            "type": type,
            "data": data or {},
        }
        ses.next_seq += 1
        ses.events.append(env)
        for q in list(ses.subscribers):
            q.put_nowait(env)
        return env

    def _set_state(self, ses: _Session, state: str) -> None:
        ses.state = state
        since = _now()
        if state == "ready":
            ses.idle_since = since
        self.emit("session_state", {"state": state, "since": since}, ses.session_id)

    async def complete_turn(self, turn_id: str, text: str = "done") -> None:
        for ses in self.sessions.values():
            turn = ses.turns.get(turn_id)
            if turn is None:
                continue
            turn.state, turn.final_text, turn.completed_at = "completed", text, _now()
            ses.turns_completed += 1
            ses.has_transcript = True
            ses.current_turn_id = None
            self.emit("assistant_text", {"text": text}, ses.session_id)
            # the real wrapper logs the idle transition before turn_completed
            self._set_state(ses, "ready")
            self.emit("turn_completed", {"record": turn.record(ses.session_id)}, ses.session_id)
            await asyncio.sleep(0)
            return
        raise KeyError(turn_id)

    def set_context(
        self,
        used_tokens: int | None = None,
        window_tokens: int | None = None,
        used_percentage: float | None = None,
        model: str = "claude-sonnet-4-5",
        session_id: str | None = None,
    ) -> None:
        """Set the snapshot context; no arguments clears it (null)."""
        ses = self._session(session_id)
        if used_tokens is None and window_tokens is None:
            ses.context = None
            return
        if used_percentage is None and used_tokens is not None and window_tokens:
            used_percentage = min(100.0, max(0.0, used_tokens / window_tokens * 100))
        ses.context = {
            "used_tokens": used_tokens,
            "window_tokens": window_tokens,
            "used_percentage": used_percentage,
            "model": model,
            "source": "statusline",
            "updated_at": _now(),
        }

    def drop_streams(self) -> None:
        """End every open SSE stream, as a network disconnect would."""
        for ses in self.sessions.values():
            for q in list(ses.subscribers):
                q.put_nowait(_CLOSE)

    def skip_seq(self, n: int, session_id: str | None = None) -> None:
        """Burn n sequence numbers, as a log rotation that lost those events would."""
        self._session(session_id).next_seq += n

    def set_booting(self, booting: bool) -> None:
        self._booting = booting

    def set_turn_status(self, code: int | None) -> None:
        """Answer every turn submission with this status (None restores normal behaviour)."""
        self._status = code

    def delay_turn_response(self, seconds: float) -> None:
        """Record the turn, then answer after `seconds` (a client timeout sees a started turn)."""
        self._delay = seconds

    def fail_turn_after_receive(self, code: int | None) -> None:
        """Record the turn, then answer the submission with this status (None restores)."""
        self._status_after = code

    def reset_connection_after_receive(self, enabled: bool = True) -> None:
        """Record the turn, then drop the connection without answering."""
        self._reset_after = enabled

    def reset_connection_before_receive(self, enabled: bool = True) -> None:
        """Drop the connection after the request arrived, recording no turn."""
        self._reset_before = enabled

    async def set_unreachable(self, unreachable: bool) -> None:
        """Stop listening (connections are refused) or listen again on the same port."""
        uv = self._uv
        if uv is None or unreachable == self._unreachable:
            return
        self._unreachable = unreachable
        if unreachable:
            for server in uv.servers:
                server.close()
            for conn in list(uv.server_state.connections):
                conn.transport.abort()
            await asyncio.sleep(0.05)  # let pooled client connections notice the reset
            return
        factory = functools.partial(
            uv.config.http_protocol_class,
            config=uv.config,
            server_state=uv.server_state,
            app_state=uv.lifespan.state,
        )
        loop = asyncio.get_running_loop()
        uv.servers = [await loop.create_server(factory, sock=self._bind(self._port))]

    def set_session_state(
        self, state: str, has_transcript: bool | None = None, session_id: str | None = None
    ) -> None:
        ses = self._session(session_id)
        if has_transcript is not None:
            ses.has_transcript = has_transcript
        if state != "busy":
            ses.current_turn_id = None
        self._set_state(ses, state)

    # ------------------------------------------------------------------ http

    def _abort(self, request: Request) -> None:
        assert self._uv is not None
        peer = tuple(request.scope["client"])
        for conn in list(self._uv.server_state.connections):
            if conn.transport.get_extra_info("peername") == peer:
                conn.transport.abort()

    def _auth(self, request: HTTPConnection) -> None:
        if request.headers.get("authorization") != f"Bearer {self.token}":
            raise _Err(401, "unauthorized", "missing or invalid bearer token")
        method = request.scope.get("method") or request.scope["type"]
        self.requests.append((method, request.url.path))

    def _get(self, session_id: str) -> _Session:
        ses = self.sessions.get(session_id)
        if ses is None:
            raise _Err(404, "not_found", f"unknown session {session_id}")
        return ses

    def _new_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids:012x}"

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @app.exception_handler(_Err)
        async def _err(_r: Request, exc: _Err) -> JSONResponse:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
            body = {"error": {"code": exc.code, "message": exc.message}}
            return JSONResponse(body, status_code=exc.status, headers=headers)

        @app.exception_handler(RequestValidationError)
        async def _invalid(_r: Request, exc: RequestValidationError) -> JSONResponse:
            body = {"error": {"code": "invalid_request", "message": str(exc.errors())[:200]}}
            return JSONResponse(body, status_code=422)

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        @app.get("/readyz")
        async def readyz() -> dict[str, str]:
            return {"status": "ready"}

        @app.get("/v1/sessions")
        async def list_sessions(request: Request, state: str | None = None) -> dict[str, Any]:
            self._auth(request)
            snaps = [s.snapshot() for s in self.sessions.values() if state in (None, s.state)]
            return {"sessions": snaps}

        @app.post("/v1/sessions", status_code=201)
        async def create_session(request: Request) -> dict[str, Any]:
            self._auth(request)
            body = await self._json_body(request)
            live = [s for s in self.sessions.values() if s.state in ("creating", "ready", "busy")]
            if len(live) >= MAX_SESSIONS:
                raise _Err(429, "too_many_sessions")
            sid = self._new_id("ses_")
            name = body.get("name")
            workdir = Path(self._tmp.name, sid).resolve()
            workdir.mkdir(exist_ok=True)
            ses = _Session(sid, name, str(workdir), None)
            ses.model = body.get("model")
            self.sessions[sid] = ses
            self._set_state(ses, "creating")
            self._set_state(ses, "ready")
            return ses.snapshot()

        @app.get("/v1/sessions/{session_id}")
        async def get_session(session_id: str, request: Request) -> dict[str, Any]:
            self._auth(request)
            return self._get(session_id).snapshot()

        @app.delete("/v1/sessions/{session_id}")
        async def delete_session(
            session_id: str, request: Request, purge: bool = False
        ) -> dict[str, Any]:
            self._auth(request)
            ses = self._get(session_id)
            if ses.state == "creating":
                raise _Err(409, "invalid_state", "the session is still creating")
            if ses.state != "stopped":
                self._set_state(ses, "stopped")
            snap = ses.snapshot()
            self._close_terminals(session_id)
            self.emit("session_closed", {}, session_id)
            for q in list(ses.subscribers):
                q.put_nowait(_CLOSE)
            if purge:
                del self.sessions[session_id]
                shutil.rmtree(ses.workdir, ignore_errors=True)
            return snap

        @app.post("/v1/sessions/{session_id}/resume")
        async def resume(session_id: str, request: Request) -> dict[str, Any]:
            self._auth(request)
            ses = self._get(session_id)
            if ses.state not in RESUMABLE:
                raise _Err(409, "not_resumable", f"state is {ses.state}")
            if not ses.has_transcript:
                raise _Err(409, "not_resumable", "no transcript (claude never took a turn)")
            self._set_state(ses, "ready")
            return ses.snapshot()

        @app.post("/v1/sessions/{session_id}/turns", status_code=202)
        async def submit_turn(session_id: str, request: Request) -> Any:
            self._auth(request)
            ses = self._get(session_id)
            body = await self._json_body(request)
            if self._reset_before:
                self._abort(request)
                return JSONResponse({}, status_code=500)
            if self._status is not None:
                code = self._status
                raise _Err(code, {409: "busy", 503: "booting"}.get(code, f"http_{code}"))
            if self._booting or ses.state == "creating":
                raise _Err(503, "booting", "the session is still booting")
            if ses.state != "ready" and ses.state != "busy":
                raise _Err(503, "dead", f"the session is {ses.state}")
            if ses.state == "busy":
                raise _Err(409, "busy", "a turn is already running")
            text, metadata = body.get("text"), body.get("metadata", {})
            if not isinstance(text, str) or not isinstance(metadata, dict):
                raise _Err(422, "invalid_request", "text must be a string, metadata an object")
            self._write_turn_files(ses, body.get("files"))
            turn = _Turn(self._new_id("turn-"), text, metadata, _now())
            ses.turns[turn.turn_id] = turn
            ses.current_turn_id = turn.turn_id
            self.received_turns += 1
            self.turn_texts.append(text)
            self.turn_metadata.append(metadata)
            self.last_turn_id = turn.turn_id
            self._set_state(ses, "busy")
            sid = ses.session_id
            self.emit(
                "user_message",
                {"turn_id": turn.turn_id, "origin": "client", "text": text, "files": []},
                sid,
            )  # noqa: E501
            self.emit(
                "turn_started", {"turn_id": turn.turn_id, "origin": "client", "text": text}, sid
            )  # noqa: E501
            if self._reset_after:
                self._abort(request)
            if self._status_after is not None:
                code = self._status_after
                raise _Err(code, f"http_{code}")
            if self._delay:
                await asyncio.sleep(self._delay)
            return {"turn_id": turn.turn_id, "state": "running"}

        @app.get("/v1/sessions/{session_id}/turns")
        async def list_turns(session_id: str, request: Request) -> dict[str, Any]:
            self._auth(request)
            return {"turns": [t.summary() for t in self._get(session_id).turns.values()]}

        @app.get("/v1/sessions/{session_id}/turns/{turn_id}")
        async def get_turn(session_id: str, turn_id: str, request: Request) -> dict[str, Any]:
            self._auth(request)
            turn = self._get(session_id).turns.get(turn_id)
            if turn is None:
                raise _Err(404, "not_found", f"unknown turn {turn_id}")
            return turn.record(session_id)

        @app.post("/v1/sessions/{session_id}/interrupt")
        async def interrupt(session_id: str, request: Request) -> dict[str, Any]:
            self._auth(request)
            ses = self._get(session_id)
            turn = ses.turns.get(ses.current_turn_id or "")
            if turn is not None:
                turn.state, turn.completed_at = "interrupted", _now()
                ses.current_turn_id = None
                self.emit("turn_failed", {"record": turn.record(session_id)}, session_id)
                self._set_state(ses, "ready")
            return ses.snapshot()

        @app.post("/v1/sessions/{session_id}/commands", status_code=202)
        async def commands(session_id: str, request: Request) -> dict[str, Any]:
            self._auth(request)
            self._get(session_id)
            body = await self._json_body(request)
            name = body.get("name") or body.get("command")
            if name not in ("compact", "context", "mcp", "reload-plugins", "reload-skills"):
                raise _Err(422, "invalid_request", f"unknown command {name}")
            self.emit("command_queued", {"name": name}, session_id)
            return {"command_id": self._new_id("cmd-"), "state": "queued"}

        @app.get("/v1/sessions/{session_id}/files")
        async def list_files(
            session_id: str, request: Request, path: str = "", offset: int = 0
        ) -> dict[str, Any]:
            self._auth(request)
            ses = self._get(session_id)
            if offset < 0:
                raise _Err(422, "invalid_request", "offset must be >= 0")
            root, resolved = self._locate(ses, path)
            if not resolved.exists():
                raise _Err(404, "not_found", "no such file or directory")
            if not resolved.is_dir():
                raise _Err(400, "not_a_directory", "path is not a directory")
            rows = []
            for entry in os.scandir(resolved):
                st = entry.stat(follow_symlinks=False)
                kind = _entry_type(st.st_mode)
                rows.append(
                    (
                        kind != "dir",
                        os.fsencode(entry.name),
                        {
                            "name": entry.name,
                            "type": kind,
                            "size": st.st_size if kind == "file" else None,
                            "mtime": _iso(st.st_mtime),
                        },
                    )
                )
            rows.sort(key=lambda row: (row[0], row[1]))
            size = self.files_max_entries
            more = offset + size < len(rows)
            return {
                "session_id": session_id,
                "root": str(root),
                "path": "" if resolved == root else resolved.relative_to(root).as_posix(),
                "entries": [row[2] for row in rows[offset : offset + size]],
                "next_offset": offset + size if more else None,
            }

        @app.get("/v1/sessions/{session_id}/files/content")
        async def file_content(session_id: str, request: Request, path: str = "") -> dict[str, Any]:
            self._auth(request)
            ses = self._get(session_id)
            root, resolved = self._locate(ses, path)
            if not resolved.exists():
                raise _Err(404, "not_found", "no such file or directory")
            st = resolved.stat()
            if not stat.S_ISREG(st.st_mode):
                raise _Err(400, "not_a_file", "path is not a regular file")
            out: dict[str, Any] = {
                "session_id": session_id,
                "path": "" if resolved == root else resolved.relative_to(root).as_posix(),
                "size": st.st_size,
                "mtime": _iso(st.st_mtime),
                "mime": mimetypes.guess_type(resolved.name)[0],
                "kind": "too_large",
                "content": None,
            }
            if st.st_size > self.file_preview_max_bytes:
                return out
            data = resolved.read_bytes()
            try:
                if b"\x00" in data[:BINARY_SNIFF_BYTES]:
                    raise UnicodeDecodeError("utf-8", b"", 0, 1, "NUL byte")
                out["content"], out["kind"] = data.decode("utf-8"), "text"
            except UnicodeDecodeError:
                out["kind"] = "binary"
            return out

        @app.post("/v1/sessions/{session_id}/terminals", status_code=201)
        async def create_terminal(session_id: str, request: Request) -> dict[str, Any]:
            self._auth(request)
            ses = self._get(session_id)
            body = await self._json_body(request)
            if set(body) - {"rows", "cols"}:
                raise _Err(422, "invalid_request", "only rows and cols are accepted")
            size = {"rows": body.get("rows", 24), "cols": body.get("cols", 80)}
            for value in size.values():
                if not isinstance(value, int) or isinstance(value, bool):
                    raise _Err(422, "invalid_request", "rows and cols must be integers")
                if value not in TERMINAL_SIZE:
                    raise _Err(422, "invalid_size", "rows and cols must be between 1 and 1000")
            if ses.state not in ACTIVE_STATES:
                raise _Err(409, "session_not_active", f"the session is {ses.state}")
            if len(self.terminals) >= self.terminals_max:
                raise _Err(409, "terminal_limit", "too many terminals")
            tid = f"term_{secrets.token_hex(6)}"  # unique across fakes, like the wrapper
            term = _Terminal(tid, session_id, **size)
            self.terminals[tid] = term
            return term.record()

        @app.get("/v1/sessions/{session_id}/terminals")
        async def list_terminals(session_id: str, request: Request) -> dict[str, Any]:
            self._auth(request)
            self._get(session_id)
            rows = [t.record() for t in self.terminals.values() if t.session_id == session_id]
            return {"terminals": rows}

        @app.delete("/v1/sessions/{session_id}/terminals/{terminal_id}", status_code=204)
        async def delete_terminal(session_id: str, terminal_id: str, request: Request) -> Response:
            self._auth(request)
            self._get(session_id)
            self._terminal_of(session_id, terminal_id).finish(-9)
            del self.terminals[terminal_id]
            return Response(status_code=204)

        @app.websocket("/v1/sessions/{session_id}/terminals/{terminal_id}/attach")
        async def attach_terminal(ws: WebSocket, session_id: str, terminal_id: str) -> None:
            try:  # denied before accept with the HTTP status the real wrapper answers
                self._auth(ws)
                ses = self._get(session_id)
                term = self._terminal_of(session_id, terminal_id)
                if ses.state not in ACTIVE_STATES:
                    raise _Err(409, "session_not_active", f"the session is {ses.state}")
            except _Err as exc:
                body = {"error": {"code": exc.code, "message": exc.message}}
                await ws.send_denial_response(JSONResponse(body, status_code=exc.status))
                return
            await ws.accept()
            await self._serve_terminal(ws, term)

        @app.get("/v1/peers")
        async def peers(request: Request) -> dict[str, Any]:
            self._auth(request)
            return self.peers

        @app.get("/v1/sessions/{session_id}/history")
        async def history(
            session_id: str,
            request: Request,
            after_seq: int = 0,
            limit: int = 100,
            before_seq: int | None = None,
            types: str | None = None,
        ) -> dict[str, Any]:
            self._auth(request)
            ses = self._get(session_id)
            if limit < 1 or after_seq < 0 or (before_seq is not None and before_seq < 0):
                raise _Err(422, "invalid_request", "bad query values")
            wanted: set[str] | None = None
            if types is not None:
                names = {t.strip() for t in types.split(",") if t.strip()}
                unknown = sorted(names - set(EVENT_TYPES))
                if unknown or not names:
                    raise _Err(400, "invalid_request", f"unknown event types: {unknown}")
                wanted = names
            if before_seq is not None and "after_seq" in request.query_params:
                raise _Err(400, "invalid_request", "before_seq and after_seq are exclusive")
            size = min(limit, MAX_PAGE)
            match = [e for e in ses.events if wanted is None or e["type"] in wanted]
            if before_seq is not None:
                older = [e for e in match if e["seq"] < before_seq]
                page, more = older[-size:], len(older) > size
                next_seq = page[-1]["seq"] if page else before_seq
                return {
                    "events": page,
                    "next_seq": next_seq,
                    "has_more": any(e["seq"] > next_seq for e in match),
                    "prev_seq": page[0]["seq"] if page and more else None,
                    "has_more_before": more,
                }
            newer = [e for e in match if e["seq"] > after_seq]
            page = newer[:size]
            return {
                "events": page,
                "next_seq": page[-1]["seq"] if page else after_seq,
                "has_more": len(newer) > size,
            }

        @app.get("/v1/sessions/{session_id}/events")
        async def events(
            session_id: str, request: Request, after_seq: int | None = None
        ) -> StreamingResponse:
            self._auth(request)
            ses = self._get(session_id)
            start = after_seq
            last_id = request.headers.get("last-event-id")
            if last_id is not None and last_id.strip():
                try:
                    start = max(0, int(last_id))
                except ValueError as exc:
                    raise _Err(
                        400, "invalid_last_event_id", "Last-Event-ID must be an integer"
                    ) from exc
            queue: asyncio.Queue[Any] = asyncio.Queue()
            ses.subscribers.add(queue)
            replay = (
                ses.events[-REPLAY:]
                if start is None
                else [e for e in ses.events if e["seq"] > start]
            )
            return StreamingResponse(
                self._stream(ses, queue, replay, start or 0),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

        return app

    def _terminal_of(self, session_id: str, terminal_id: str) -> _Terminal:
        if not TERMINAL_ID_RE.fullmatch(terminal_id):
            raise _Err(422, "invalid_request", "invalid terminal id")
        term = self.terminals.get(terminal_id)
        if term is None or term.session_id != session_id:
            raise _Err(404, "terminal_not_found", f"unknown terminal {terminal_id}")
        return term

    def _close_terminals(self, session_id: str) -> None:
        for tid, term in list(self.terminals.items()):
            if term.session_id == session_id:
                term.finish(-9)
                del self.terminals[tid]

    async def _serve_terminal(self, ws: WebSocket, term: _Terminal) -> None:
        """The wrapper attach protocol: replay, echo input as output, resize/ping, exit frame."""
        for start in range(0, len(term.scrollback), TERMINAL_MAX_FRAME_BYTES):
            await ws.send_bytes(bytes(term.scrollback[start : start + TERMINAL_MAX_FRAME_BYTES]))
        if not term.alive:
            await ws.send_text(json.dumps({"type": "exit", "code": term.exit_code}))
            await ws.close(1000)
            return
        queue: asyncio.Queue[Any] = asyncio.Queue()
        term.clients.add(queue)
        pump = asyncio.create_task(self._pump(ws, queue))
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    if len(msg["bytes"]) > TERMINAL_MAX_FRAME_BYTES:
                        queue.put_nowait(1009)
                        continue
                    term.output(msg["bytes"])
                elif not self._control(term, msg.get("text") or "", queue):
                    queue.put_nowait(1003)
        finally:
            term.clients.discard(queue)
            pump.cancel()

    @staticmethod
    async def _pump(ws: WebSocket, queue: asyncio.Queue[Any]) -> None:
        """Send queued items: bytes as binary, dicts as JSON text, an int closes with that code."""
        while True:
            item = await queue.get()
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            elif isinstance(item, dict):
                await ws.send_text(json.dumps(item))
            else:
                await ws.close(item)
                return

    @staticmethod
    def _control(term: _Terminal, text: str, queue: asyncio.Queue[Any]) -> bool:
        """Handle one JSON control frame; False for a frame the protocol does not know."""
        try:
            frame = json.loads(text)
        except ValueError:
            return False
        kind = frame.get("type") if isinstance(frame, dict) else None
        if kind == "ping":
            queue.put_nowait({"type": "pong"})
        elif kind == "resize":
            rows, cols = frame.get("rows"), frame.get("cols")
            if all(
                isinstance(v, int) and not isinstance(v, bool) and v in TERMINAL_SIZE
                for v in (rows, cols)
            ):
                term.rows, term.cols = rows, cols
            else:
                log.warning("ignored bad resize frame %r", frame)
        else:
            return False
        return True

    @staticmethod
    def _locate(ses: _Session, rel: str) -> tuple[Path, Path]:
        """(root, resolved): a vanished workdir is not_found, an escape invalid_path."""
        if not os.path.isdir(ses.workdir):
            raise _Err(404, "not_found", "the session workdir does not exist")
        return Path(os.path.realpath(ses.workdir)), _confine(Path(ses.workdir), rel)

    def _write_turn_files(self, ses: _Session, files: Any) -> None:
        """The turn `files` field: relative path to text, written into the workdir first."""
        if files is None:
            return
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            raise _Err(422, "invalid_request", "files must map paths to text")
        for rel, content in files.items():
            target = _confine(Path(ses.workdir), rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    @staticmethod
    async def _json_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            return {}
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise _Err(422, "invalid_request", "body is not JSON") from exc
        if not isinstance(body, dict):
            raise _Err(422, "invalid_request", "body must be an object")
        return body

    @staticmethod
    def _frame(env: dict[str, Any], with_id: bool = True) -> bytes:
        head = f"id: {env['seq']}\n" if with_id else ""
        data = json.dumps(env, separators=(",", ":"))
        return f"{head}event: {env['type']}\ndata: {data}\n\n".encode()

    async def _stream(
        self,
        ses: _Session,
        queue: asyncio.Queue[Any],
        replay: list[dict[str, Any]],
        sent: int,
    ) -> AsyncIterator[bytes]:
        try:
            for env in replay:
                sent = max(sent, env["seq"])
                yield self._frame(env)
                if env["type"] == "session_closed":
                    return
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), self.ping_interval)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                if item is _CLOSE:
                    return
                if item["seq"] <= sent:
                    continue
                sent = item["seq"]
                yield self._frame(item)
                if item["type"] == "session_closed":
                    return
        finally:
            ses.subscribers.discard(queue)
            with contextlib.suppress(Exception):
                queue.put_nowait(_CLOSE)
