"""Bench spec: arms, tasks, repeats, timeout and optional chaos."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agents_orchestrator.spec import ClusterSpec

_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


class BenchSpecError(ValueError):
    """An invalid bench spec; `errors` holds one `field: message` per violation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid bench spec: " + "; ".join(errors))


class Arm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME, max_length=24)
    replicas: int = Field(ge=1, le=16)
    peers: bool
    max_replicas: int | None = Field(default=None, ge=1, le=16)
    static: bool | None = None


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME)
    prompt: str = Field(min_length=1)
    verify: str = Field(min_length=1)


class Chaos(BaseModel):
    model_config = ConfigDict(extra="forbid")

    after_seconds: float = Field(ge=0)


class BenchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arms: list[Arm] = Field(min_length=1)
    cluster: ClusterSpec = ClusterSpec()
    tasks: list[Task] = Field(min_length=1)
    repeats: int = Field(default=1, ge=1, le=1000)
    timeout_seconds: float = Field(default=1800, gt=0)
    chaos: Chaos | None = None

    @model_validator(mode="after")
    def _unique(self) -> BenchSpec:
        for field, items in (("arms", self.arms), ("tasks", self.tasks)):
            names = [i.name for i in items]
            dup = sorted({n for n in names if names.count(n) > 1})
            if dup:
                raise ValueError(f"{field}: duplicate names {', '.join(dup)}")
        return self


def _format(exc: ValidationError) -> list[str]:
    return [
        ".".join(str(p) for p in e["loc"]) + ": " + e["msg"] if e["loc"] else e["msg"]
        for e in exc.errors()
    ]


def parse_spec(data: Any) -> BenchSpec:
    if not isinstance(data, dict):
        raise BenchSpecError(["spec: must be a mapping"])
    try:
        return BenchSpec.model_validate(data)
    except ValidationError as exc:
        raise BenchSpecError(_format(exc)) from None


def load_spec(path: str | Path) -> BenchSpec:
    try:
        data = yaml.safe_load(Path(path).read_text())
    except OSError as exc:
        raise BenchSpecError([f"spec: cannot read {path}: {exc.strerror}"]) from None
    except yaml.YAMLError as exc:
        raise BenchSpecError([f"spec: invalid YAML: {exc}"]) from None
    return parse_spec(data)
