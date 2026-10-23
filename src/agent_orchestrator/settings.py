"""Validated configuration for every orchestrator process (env prefix ORCH_)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    master_key: str | None = None
    master_key_file: str = ""
    namespace: str = "agents"
    agent_image: str = ""
    check_image: str = ""

    default_model: str = ""
    default_effort: str = ""
    models: CsvList = []
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
                    problems.setdefault(_var(str(loc[0])), msg)
                else:
                    problems.setdefault("ORCH_*", msg)
            # cross-field errors carry their own variable names
            for name, msg in list(problems.items()):
                if name == "ORCH_*" and ":" in msg:
                    var, _, rest = msg.partition(": ")
                    del problems[name]
                    problems[var] = rest
            raise SettingsError(problems) from None

    _split = field_validator("models", "allowed_cidrs", "agent_images", mode="before")(
        lambda cls, v: _csv(v)
    )

    @field_validator("allowed_cidrs")
    @classmethod
    def _cidrs(cls, v: list[str]) -> list[str]:
        for entry in v:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(f"unparsable CIDR {entry!r}") from exc
        return v

    @field_validator("any_mcp_ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        if not _TAG_RE.match(v):
            raise ValueError(f"{v!r} must be a pinned release tag like v1.2.3, not a branch")
        return v

    @model_validator(mode="after")
    def _resolve_master_key(self) -> Settings:
        if self.master_key_file:
            try:
                self.master_key = Path(self.master_key_file).read_text()
            except OSError as exc:
                msg = f"ORCH_MASTER_KEY_FILE: cannot read {self.master_key_file}: {exc}"
                raise _Bad(msg) from exc
        if self.master_key is not None:
            self.master_key = self.master_key.strip()
            if len(self.master_key.encode()) < MIN_MASTER_KEY_BYTES:
                var = "ORCH_MASTER_KEY_FILE" if self.master_key_file else "ORCH_MASTER_KEY"
                raise _Bad(f"{var}: master key must be at least {MIN_MASTER_KEY_BYTES} bytes")
        if not self.check_image:
            self.check_image = self.agent_image
        return self

    @model_validator(mode="after")
    def _timing(self) -> Settings:
        if 2 * self.wrapper_send_timeout_seconds > self.dispatching_stale_seconds:
            raise _Bad(
                "ORCH_WRAPPER_SEND_TIMEOUT_SECONDS: must be at most half of "
                "ORCH_DISPATCHING_STALE_SECONDS"
            )
        if self.ambiguous_settle_seconds < 2 * self.wrapper_send_timeout_seconds:
            raise _Bad(
                "ORCH_AMBIGUOUS_SETTLE_SECONDS: must be at least twice "
                "ORCH_WRAPPER_SEND_TIMEOUT_SECONDS"
            )
        if self.ambiguous_deadline_seconds <= self.ambiguous_settle_seconds:
            raise _Bad("ORCH_AMBIGUOUS_DEADLINE_SECONDS: must exceed ORCH_AMBIGUOUS_SETTLE_SECONDS")
        if self.retry_backoff_base_seconds <= 0:
            raise _Bad("ORCH_RETRY_BACKOFF_BASE_SECONDS: must be greater than 0")
        if self.retry_backoff_max_seconds < self.retry_backoff_base_seconds:
            raise _Bad(
                "ORCH_RETRY_BACKOFF_MAX_SECONDS: must be at least ORCH_RETRY_BACKOFF_BASE_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _hook(self) -> Settings:
        if self.discard_hook_command and self.discard_hook_url:
            raise _Bad(
                "ORCH_DISCARD_HOOK_URL: set at most one of ORCH_DISCARD_HOOK_COMMAND "
                "and ORCH_DISCARD_HOOK_URL"
            )
        if self.discard_hook_url:
            parsed = urlparse(self.discard_hook_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise _Bad("ORCH_DISCARD_HOOK_URL: must be an http or https URL")
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

    def discard_hook(self) -> DiscardHook | None:
        if self.discard_hook_command:
            return DiscardHook("command", self.discard_hook_command)
        if self.discard_hook_url:
            return DiscardHook("url", self.discard_hook_url)
        return None
