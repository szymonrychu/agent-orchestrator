"""Activity digest: pure functions over stored `events` rows, serving `GET /agent/v1/activity`.

A row is a mapping with `id`, `type`, `data`, `ts` (datetime) and `agent` (the pod name).

Only paths, Bash commands with exit status and finished-turn first lines get through. Every
peer-originated string is XML-escaped and single-line, so it cannot close or forge a
`<hive-state>` block or any other tag in the text an agent reads. The same `_clean` escapes the
member fields of the pushed `hive_state` note (`dispatcher/hive.py`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

PENDING_BASH_TIMEOUT_SECONDS = 60
MAX_TEXT = 200
_EDIT_TOOLS = {"Write": "wrote", "Edit": "edited", "MultiEdit": "edited", "NotebookEdit": "edited"}


@dataclass(frozen=True)
class FeedLine:
    id: int
    agent: str
    text: str


def _clean(value: Any, limit: int | None = None) -> str:
    text = " ".join(str(value if value is not None else "").split())
    if limit is not None:
        text = text[:limit]
    return escape(text)


def _relative(path: str, workdir: str) -> str:
    base = workdir.rstrip("/")
    if base and path.startswith(base + "/"):
        return path[len(base) + 1 :]
    return path


def _status(result: Mapping[str, Any]) -> str:
    code = result.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool):
        return "ok" if code == 0 and not result.get("is_error") else f"exit {code}"
    return "failed" if result.get("is_error") else "ok"


def digest_lines(
    rows: Iterable[Mapping[str, Any]], now: datetime, workdir: str = ""
) -> list[FeedLine]:
    """Feed lines for `rows` (ascending id); a Bash call still awaiting a result gets none."""
    rows = list(rows)
    results = {
        (r.get("agent"), (r.get("data") or {}).get("tool_use_id")): r["data"]
        for r in rows
        if r["type"] == "tool_result"
    }
    lines: list[FeedLine] = []
    for row in rows:
        data = row.get("data") or {}
        kind, agent = row["type"], _clean(row.get("agent"))
        if kind == "tool_call":
            tool, tin = data.get("tool_name"), data.get("tool_input") or {}
            if tool in _EDIT_TOOLS:
                path = tin.get("file_path") or tin.get("notebook_path")
                if path:
                    text = f"{agent} {_EDIT_TOOLS[tool]} {_clean(_relative(str(path), workdir))}"
                    lines.append(FeedLine(row["id"], agent, text))
            elif tool == "Bash" and tin.get("command") is not None:
                cmd = f"{agent} ran: {_clean(tin['command'], MAX_TEXT)}"
                res = results.get((row.get("agent"), data.get("tool_use_id")))
                if res is not None:
                    lines.append(FeedLine(row["id"], agent, f"{cmd} ({_status(res)})"))
                elif (now - row["ts"]).total_seconds() > PENDING_BASH_TIMEOUT_SECONDS:
                    lines.append(FeedLine(row["id"], agent, cmd))
        elif kind == "turn_completed":
            record = data.get("record") or {}
            first = next(iter(str(record.get("final_text") or "").strip().splitlines()), "")
            text = f"{agent} finished a turn: {_clean(first, MAX_TEXT)}"
            lines.append(FeedLine(row["id"], agent, text.rstrip()))
    return lines
