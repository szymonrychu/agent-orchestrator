"""Incremental Server-Sent Events parser."""

from __future__ import annotations

import codecs
from dataclasses import dataclass


@dataclass(frozen=True)
class SSEEvent:
    id: str | None
    event: str
    data: str
    retry: int | None = None


class SSEParser:
    """Feed bytes or text in any chunking; complete events come back as they dispatch."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buf = ""
        self._last_id: str | None = None
        self._reset()

    def _reset(self) -> None:
        self._event = ""
        self._data: list[str] = []
        self._retry: int | None = None

    def feed(self, chunk: bytes | str) -> list[SSEEvent]:
        self._buf += self._decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        out: list[SSEEvent] = []
        buf, pos = self._buf, 0
        while True:
            i = next((k for k in range(pos, len(buf)) if buf[k] in "\r\n"), -1)
            if i < 0 or (buf[i] == "\r" and i == len(buf) - 1):
                break  # no full line yet, or a CR that may be the first half of CRLF
            line = buf[pos:i]
            pos = i + 2 if buf[i : i + 2] == "\r\n" else i + 1
            ev = self._line(line)
            if ev is not None:
                out.append(ev)
        self._buf = buf[pos:]
        return out

    def _line(self, line: str) -> SSEEvent | None:
        if not line:
            if not self._data:
                self._reset()
                return None
            ev = SSEEvent(
                self._last_id, self._event or "message", "\n".join(self._data), self._retry
            )
            self._reset()
            return ev
        if line.startswith(":"):
            return None
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "data":
            self._data.append(value)
        elif name == "event":
            self._event = value
        elif name == "id":
            if "\0" not in value:
                self._last_id = value
        elif name == "retry" and value.isdigit():
            self._retry = int(value)
        return None
