"""Validated configuration for every orchestrator process (env prefix ORCH_)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from agent_orchestrator.spec import parse_quantity

PREFIX = "ORCH_"
MIN_MASTER_KEY_BYTES = 32
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

CsvList = Annotated[list[str], NoDecode]


class SettingsError(ValueError):
    """One error naming every bad ORCH_* variable."""

    def __init__(self, problems: dict[str, str]):
        self.problems = problems
        super().__init__(
            "invalid configuration: " + "; ".join(f"{k}: {v}" for k, v in problems.items())
        )


@dataclass(frozen=True)
class DiscardHook:
    kind: Literal["command", "url"]
    target: str


def _var(field: str) -> str:
    return PREFIX + field.upper()


def _csv(value: object) -> object:
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return value


class _Bad(ValueError):
    """Validation failure that already names its variable(s)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=PREFIX, extra="ignore")

    database_url: str
    master_key_file: str = ""
    master_key: str | None = None
    namespace: str = "agents"
    agent_image: str = ""
    check_image: str = ""

    default_model: str = ""  # empty = the first entry of models
    default_effort: str = ""
    models: CsvList = [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-1",
    ]
    efforts: CsvList = ["low", "medium", "high", "xhigh", "max"]
    default_cpu_request: str = "500m"
    default_cpu_limit: str = "4"
    default_memory_request: str = "1Gi"
    default_memory_limit: str = "8Gi"
    default_replicas: int = 1
    default_auto_compact_pct: int = 95

    pvc_storage_class: str = "rook-ceph-rwx"
    pvc_size: str = "20Gi"
    claude_secret_name: str = "agent-orchestrator-claude"
    image_pull_secret: str = "regcred"
    any_mcp_config_file: str = ""
    wrapper_port: int = 8080
    cluster_domain: str = "cluster.local"
    agent_termination_grace_seconds: int = 90

    http_host: str = "0.0.0.0"
    http_port: int = 8000
    static_dir: str = ""
    dispatch_poll_seconds: float = 2
    reconcile_poll_seconds: float = 10
    ingest_refresh_seconds: float = 5
    shutdown_grace_seconds: float = 20
    log_level: str = "INFO"

    allowed_cidrs: CsvList = ["192.168.1.0/24"]
    agent_images: CsvList = []
    max_replicas: int = 16
    max_pvc_size: str = "100Gi"
    max_clusters: int = 20

    wrapper_send_timeout_seconds: float = 15
    dispatching_stale_seconds: float = 60
    dispatching_heartbeat_seconds: float = 5
    lock_heartbeat_seconds: float = 5
    lock_tcp_user_timeout_seconds: float = 15

    retry_backoff_base_seconds: float = 2
    retry_backoff_max_seconds: float = 60
    ambiguous_settle_seconds: float = 60
    ambiguous_deadline_seconds: float = 600

    discard_hook_command: str = ""
    discard_hook_url: str = ""
    discard_hook_timeout_seconds: float = 120
    discard_quiesce_timeout_seconds: float = 120
    discard_drain_seconds: float = 5

    any_mcp_repo: str = "https://github.com/szymonrychu/any-mcp"
    any_mcp_ref: str = "v0.1.0"
    on_start_timeout_seconds: float = 600

    def __init__(self, **values: object) -> None:
        try:
            super().__init__(**values)
        except ValidationError as exc:
            problems: dict[str, str] = {}
            for err in exc.errors():
                loc = err["loc"]
                msg = err["msg"].removeprefix("Value error, ")
                if loc:
                    # "ORCH_X::msg" names a different variable than the failing field
                    var, sep, rest = msg.partition("::")
                    if sep and var.startswith(PREFIX):
                        problems.setdefault(var, rest)
                    else:
                        problems.setdefault(_var(str(loc[0])), msg)
                else:
                    problems.setdefault("ORCH_*", msg)
            raise SettingsError(problems) from None

    _split = field_validator("models", "efforts", "allowed_cidrs", "agent_images", mode="before")(
        lambda cls, v: _csv(v)
    )

    # Checks are field validators (not one mode="after" model validator) so they still run
    # when an unrelated field fails type or required checks; cross-field checks skip
    # silently when a dependency already failed (that failure is reported on its own).

    @field_validator("allowed_cidrs")
    @classmethod
    def _check_cidrs(cls, v: list[str]) -> list[str]:
        bad = []
        for entry in v:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                bad.append(entry)
        if bad:
            raise _Bad("unparsable CIDR " + ", ".join(repr(c) for c in bad))
        return v

    @field_validator(
        "default_cpu_request",
        "default_cpu_limit",
        "default_memory_request",
        "default_memory_limit",
    )
    @classmethod
    def _check_quantity(cls, v: str) -> str:
        try:
            positive = parse_quantity(v) > 0
        except ValueError as exc:
            raise _Bad(str(exc)) from None
        if not positive:
            raise _Bad(f"{v!r} must be a positive quantity")
        return v

    @field_validator("efforts")
    @classmethod
    def _check_default_effort(cls, v: list[str], info: ValidationInfo) -> list[str]:
        effort = info.data.get("default_effort")
        if effort and effort not in v:
            raise _Bad(f"ORCH_DEFAULT_EFFORT::{effort!r} is not one of ORCH_EFFORTS")
        return v

    @field_validator("models")
    @classmethod
    def _check_default_model(cls, v: list[str], info: ValidationInfo) -> list[str]:
        if not v:
            raise _Bad("must list at least one model")
        model = info.data.get("default_model")
        if model and model not in v:
            raise _Bad(f"ORCH_DEFAULT_MODEL::{model!r} is not one of ORCH_MODELS")
        return v

    @field_validator("default_memory_limit")
    @classmethod
    def _check_request_within_limit(cls, v: str, info: ValidationInfo) -> str:
        data = info.data
        for res, req_key, lim in (
            ("CPU", "default_cpu_request", data.get("default_cpu_limit")),
            ("MEMORY", "default_memory_request", v),
        ):
            req = data.get(req_key)
            if req is None or lim is None:
                continue
            try:
                over = parse_quantity(req) > parse_quantity(lim)
            except ValueError:
                continue  # reported on its own
            if over:
                raise _Bad(f"ORCH_DEFAULT_{res}_REQUEST::must not exceed ORCH_DEFAULT_{res}_LIMIT")
        return v

    @field_validator("any_mcp_ref")
    @classmethod
    def _check_ref(cls, v: str) -> str:
        if not _TAG_RE.match(v):
            raise _Bad(f"{v!r} must be a pinned release tag like v1.2.3, not a branch")
        return v

    @field_validator("master_key_file")
    @classmethod
    def _check_key_file(cls, v: str) -> str:
        if v:
            try:
                key = Path(v).read_text().strip()
            except OSError as exc:
                raise _Bad(f"cannot read {v}: {exc}") from None
            if len(key.encode()) < MIN_MASTER_KEY_BYTES:
                raise _Bad(f"master key must be at least {MIN_MASTER_KEY_BYTES} bytes")
        return v

    @field_validator("master_key")
    @classmethod
    def _check_key(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None or info.data.get("master_key_file"):
            return v  # the file wins and is validated on its own
        v = v.strip()
        if len(v.encode()) < MIN_MASTER_KEY_BYTES:
            raise _Bad(f"master key must be at least {MIN_MASTER_KEY_BYTES} bytes")
        return v

    @field_validator("dispatching_stale_seconds")
    @classmethod
    def _check_stale(cls, v: float, info: ValidationInfo) -> float:
        send = info.data.get("wrapper_send_timeout_seconds")
        if send is not None and 2 * send > v:
            raise _Bad(
                "ORCH_WRAPPER_SEND_TIMEOUT_SECONDS::must be at most half of "
                "ORCH_DISPATCHING_STALE_SECONDS"
            )
        return v

    @field_validator("ambiguous_settle_seconds")
    @classmethod
    def _check_settle(cls, v: float, info: ValidationInfo) -> float:
        send = info.data.get("wrapper_send_timeout_seconds")
        if send is not None and v < 2 * send:
            raise _Bad("must be at least twice ORCH_WRAPPER_SEND_TIMEOUT_SECONDS")
        return v

    @field_validator("ambiguous_deadline_seconds")
    @classmethod
    def _check_deadline(cls, v: float, info: ValidationInfo) -> float:
        settle = info.data.get("ambiguous_settle_seconds")
        if settle is not None and v <= settle:
            raise _Bad("must exceed ORCH_AMBIGUOUS_SETTLE_SECONDS")
        return v

    @field_validator("retry_backoff_base_seconds")
    @classmethod
    def _check_base(cls, v: float) -> float:
        if v <= 0:
            raise _Bad("must be greater than 0")
        return v

    @field_validator("retry_backoff_max_seconds")
    @classmethod
    def _check_max(cls, v: float, info: ValidationInfo) -> float:
        base = info.data.get("retry_backoff_base_seconds")
        if base is not None and v < base:
            raise _Bad("must be at least ORCH_RETRY_BACKOFF_BASE_SECONDS")
        return v

    @field_validator("discard_hook_url")
    @classmethod
    def _check_hook_url(cls, v: str, info: ValidationInfo) -> str:
        if info.data.get("discard_hook_command") and v:
            raise _Bad("set at most one of ORCH_DISCARD_HOOK_COMMAND and ORCH_DISCARD_HOOK_URL")
        if v:
            parsed = urlparse(v)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise _Bad("must be an http or https URL")
        return v

    @model_validator(mode="after")
    def _resolve(self) -> Settings:
        if self.master_key_file:
            self.master_key = Path(self.master_key_file).read_text().strip()
        if not self.default_model:
            self.default_model = self.models[0]
        if not self.check_image:
            self.check_image = self.agent_image
        return self

    def require_master_key(self) -> str:
        if not self.master_key:
            raise SettingsError({"ORCH_MASTER_KEY": "required (or ORCH_MASTER_KEY_FILE)"})
        return self.master_key

    def require_agent_image(self) -> str:
        if not self.agent_image:
            raise SettingsError({"ORCH_AGENT_IMAGE": "required"})
        return self.agent_image

    def retry_delay_seconds(self, attempts: int) -> float:
        return min(
            self.retry_backoff_base_seconds * 2 ** (attempts - 1), self.retry_backoff_max_seconds
        )

    def allowed_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(c, strict=False) for c in self.allowed_cidrs]

    def image_allowlist(self) -> frozenset[str]:
        extra = set(self.agent_images)
        if self.agent_image:
            extra.add(self.agent_image)
        return frozenset(extra)

    def default_any_mcp_config(self) -> dict[str, Any]:
        """The any-mcp config clusters get when their spec carries none."""
        if self.any_mcp_config_file:
            return yaml.safe_load(Path(self.any_mcp_config_file).read_text()) or {}
        return {}

    def default_resources(self) -> dict[str, dict[str, str]]:
        return {
            "requests": {"cpu": self.default_cpu_request, "memory": self.default_memory_request},
            "limits": {"cpu": self.default_cpu_limit, "memory": self.default_memory_limit},
        }

    def discard_hook(self) -> DiscardHook | None:
        if self.discard_hook_command:
            return DiscardHook("command", self.discard_hook_command)
        if self.discard_hook_url:
            return DiscardHook("url", self.discard_hook_url)
        return None
