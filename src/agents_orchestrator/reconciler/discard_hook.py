"""Run the pre-discard hook (D15): a settings command or a webhook, fed the JSONL export.

The command comes only from settings (operator-controlled): the API has no auth and the
reconciler holds the Kubernetes credentials, so a spec-level command would be remote code
execution. The spec carries only a webhook URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from agents_orchestrator.db.sessions import Session
from agents_orchestrator.settings import DiscardHook, Settings

STDERR_TAIL_BYTES = 1024
WEBHOOK_REASON_BYTES = 512


@dataclass(frozen=True)
class HookResult:
    ok: bool
    reason: str = ""


def resolve_hook(session: Session, settings: Settings) -> DiscardHook | None:
    """The session's own webhook URL wins over the settings hook."""
    if session.spec.discard_hook_url:
        return DiscardHook("url", session.spec.discard_hook_url)
    return settings.discard_hook()


async def run_discard_hook(
    hook: DiscardHook,
    session: Session,
    lines: AsyncIterator[str],
    timeout: float,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HookResult:
    try:
        async with asyncio.timeout(timeout):
            if hook.kind == "command":
                return await _run_command(hook.target, session, lines)
            return await _post_webhook(hook.target, session, lines, transport)
    except TimeoutError:
        return HookResult(False, f"timed out after {timeout:g}s")


async def _run_command(command: str, session: Session, lines: AsyncIterator[str]) -> HookResult:
    env = {**os.environ, "ORCH_SESSION_ID": str(session.id), "ORCH_SESSION_SLUG": session.slug}
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,  # its own process group, so a timeout kills the children too
    )
    tail = bytearray()

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(4096):
            tail.extend(chunk)
            del tail[:-STDERR_TAIL_BYTES]

    async def feed() -> None:
        assert proc.stdin is not None
        try:
            async for line in lines:
                proc.stdin.write(line.encode())
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the hook stopped reading; its exit code decides
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                proc.stdin.close()
                await proc.stdin.wait_closed()

    stderr_task = asyncio.create_task(drain_stderr())
    try:
        await feed()
        code = await proc.wait()
        await stderr_task
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        stderr_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr_task
        await proc.wait()
        raise
    if code == 0:
        return HookResult(True)
    stderr = tail.decode(errors="replace").strip()
    return HookResult(False, f"exit {code}: {stderr}" if stderr else f"exit {code}")


async def _post_webhook(
    url: str,
    session: Session,
    lines: AsyncIterator[str],
    transport: httpx.AsyncBaseTransport | None,
) -> HookResult:
    async def body() -> AsyncIterator[bytes]:
        async for line in lines:
            yield line.encode()

    headers = {
        "Content-Type": "application/x-ndjson",
        "X-Orch-Session-Id": str(session.id),
        "X-Orch-Session-Slug": session.slug,
    }
    try:
        async with (
            httpx.AsyncClient(transport=transport, timeout=None) as client,
            client.stream("POST", url, content=body(), headers=headers) as response,
        ):
            if response.is_success:
                return HookResult(True)
            text = (await response.aread()).decode(errors="replace").strip()
            reason = f"status {response.status_code}"
            if text:
                reason += f": {text[:WEBHOOK_REASON_BYTES]}"
            return HookResult(False, reason)
    except httpx.HTTPError as exc:
        return HookResult(False, f"{type(exc).__name__}: {exc}")
