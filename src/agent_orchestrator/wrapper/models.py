"""Typed views of the wrapper /v1 API. Unknown fields are ignored."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Context(_Model):
    used_tokens: int | None = None
    window_tokens: int | None = None
    used_percentage: float | None = None
    model: str | None = None
    source: str | None = None
    updated_at: datetime | None = None


class Snapshot(_Model):
    session_id: str
    name: str | None = None
    state: str
    current_turn_id: str | None = None
    idle_since: datetime | None = None
    context: Context | None = None
    model: str | None = None
    workdir: str | None = None
    turns_completed: int = 0
    # The wrapper does not report transcript presence; resume needs one, and claude writes it
    # on its first turn. An explicit field wins if the wrapper ever adds it.
    has_transcript: bool | None = None

    def model_post_init(self, _ctx: Any) -> None:
        if self.has_transcript is None:
            self.has_transcript = self.turns_completed > 0


class TurnSummary(_Model):
    turn_id: str
    origin: str | None = None
    state: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    stop_reason: str | None = None


class TurnRecord(TurnSummary):
    session_id: str | None = None
    text: str | None = None
    final_text: str | None = None
    error: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Envelope(_Model):
    seq: int
    ts: str
    session_id: str
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class PeerSession(_Model):
    session_id: str
    name: str | None = None
    cwd: str | None = None


class PeerSelf(_Model):
    instance: str
    url: str


class PeerMember(_Model):
    instance: str
    url: str
    sessions: list[PeerSession] = Field(default_factory=list)
    last_seen_at: datetime | None = None
    missed: int = 0


class PeersView(_Model):
    enabled: bool
    self: PeerSelf | None = None
    members: list[PeerMember] = Field(default_factory=list)


@dataclass(frozen=True)
class TurnAccepted:
    turn_id: str


@dataclass(frozen=True)
class Busy:
    pass


@dataclass(frozen=True)
class Gone:
    reason: str = ""


@dataclass(frozen=True)
class ConfigError:
    reason: str = ""


@dataclass(frozen=True)
class Rejected:
    reason: str = ""


@dataclass(frozen=True)
class Unavailable:
    """Nothing was delivered: safe to retry."""

    reason: str = ""


@dataclass(frozen=True)
class Ambiguous:
    """The request may have been delivered: verify before any retry."""

    reason: str = ""


SubmitOutcome = TurnAccepted | Busy | Gone | ConfigError | Rejected | Unavailable | Ambiguous


class WrapperError(Exception):
    """The wrapper answered with an error."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class WrapperUnreachable(Exception):
    """No usable answer: connect error, timeout or reset."""
