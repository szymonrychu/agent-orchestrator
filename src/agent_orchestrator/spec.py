"""ClusterSpec model and the validation that needs server settings."""

from __future__ import annotations

import re
import secrets
import string
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from agent_orchestrator.settings import Settings

SLUG_MAX = 40
RESERVED_PLUGIN = "orchestrator"
_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_RESERVED_ENV_PREFIXES = ("CCW_", "CLAUDE_", "ANTHROPIC_")
_RESERVED_ENV_SUFFIXES = ("_PROXY", "_BASE_URL")
_QUANTITY_RE = re.compile(
    r"([+-]?(?:\d+\.?\d*|\.\d+))(Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE]|[eE][+-]?\d+)?"
)
_SUFFIX: dict[str, Decimal] = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "": Decimal(1),
    "k": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(2**10),
    "Mi": Decimal(2**20),
    "Gi": Decimal(2**30),
    "Ti": Decimal(2**40),
    "Pi": Decimal(2**50),
    "Ei": Decimal(2**60),
}


class SpecInvalid(ValueError):
    """A spec that breaks server-side rules; `errors` names every violation."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid cluster spec: " + "; ".join(errors))


def slugify(text: str, max_len: int = SLUG_MAX) -> str:
    """Lowercase `text` to a DNS-1123 label body of at most `max_len`; "" if nothing is usable."""
    body = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return body[:max_len].rstrip("-")


def _random_suffix() -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(4))


def generated_slug(name: str, rand: Callable[[], str] | None = None) -> str:
    """`<base>-<4 random letters>` from a display name, always valid and at most SLUG_MAX."""
    room = SLUG_MAX - 5
    base = slugify(name, room) or "cluster"
    if base == "orchestrator" or base.startswith("orchestrator-"):
        base = slugify(f"p-{base}", room)
    return f"{base}-{(rand or _random_suffix)()}"


def validate_slug(slug: str) -> None:
    if not isinstance(slug, str) or len(slug) > SLUG_MAX or not _LABEL_RE.fullmatch(slug):
        raise ValueError(f"slug {slug!r} must be a DNS-1123 label of at most {SLUG_MAX} characters")
    if slug == "orchestrator" or slug.startswith("orchestrator-"):
        raise ValueError(f"slug {slug!r} is reserved (orchestrator, orchestrator-*)")


def parse_quantity(value: str) -> Decimal:
    """Parse a Kubernetes quantity (e.g. 500m, 20Gi, 1e3) into a Decimal."""
    m = _QUANTITY_RE.fullmatch(value) if isinstance(value, str) else None
    if not m:
        raise ValueError(f"{value!r} is not a Kubernetes quantity")
    number, suffix = m.group(1), m.group(2) or ""
    try:
        if suffix[:1] in ("e", "E") and suffix not in _SUFFIX:
            return Decimal(number + suffix)
        return Decimal(number) * _SUFFIX[suffix]
    except (InvalidOperation, KeyError):
        raise ValueError(f"{value!r} is not a Kubernetes quantity") from None


def _quantity(value: str) -> str:
    if parse_quantity(value) <= 0:
        raise ValueError(f"{value!r} must be a positive quantity")
    return value


Quantity = Annotated[str, Field(min_length=1)]


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: dict[str, str] = {}
    limits: dict[str, str] = {}

    @field_validator("requests", "limits")
    @classmethod
    def _cpu_memory(cls, v: dict[str, str]) -> dict[str, str]:
        for key, qty in v.items():
            if key not in ("cpu", "memory"):
                raise ValueError(f"only cpu and memory are allowed, got {key!r}")
            _quantity(qty)
        return v


class ClusterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replicas: int = Field(default=1, ge=1, le=16)
    max_replicas: int | None = Field(default=None, ge=1, le=16)
    static: bool = False
    image: str | None = None
    model: str | None = None
    effort: str | None = None
    peers: bool = True
    workdir: str = Field(default="workdir", max_length=63)
    pvc_size: Quantity | None = None
    resources: Resources | None = None
    env: dict[str, str] = {}
    any_mcp_config: dict[str, Any] | None = None
    max_attempts: int = Field(default=5, ge=1, le=10)
    auto_compact_pct: int | None = Field(default=None, ge=1, le=100)
    discard_hook_url: str | None = None

    @model_validator(mode="after")
    def _cap(self) -> ClusterSpec:
        if self.max_replicas is None:
            # resolved without marking the field set, so presets stay sparse
            self.__dict__["max_replicas"] = self.replicas
        elif self.replicas > self.max_replicas:
            raise ValueError(f"replicas {self.replicas} exceeds max_replicas {self.max_replicas}")
        if self.static and self.max_replicas != self.replicas:
            raise ValueError("static requires max_replicas to equal replicas")
        return self

    @property
    def hive(self) -> bool:
        """Every cluster that may run more than one agent is a hive."""
        return (self.max_replicas or self.replicas) > 1

    @property
    def alone(self) -> bool:
        return not self.hive

    @property
    def scalable(self) -> bool:
        return self.hive and not self.static

    @field_validator("workdir")
    @classmethod
    def _workdir(cls, v: str) -> str:
        if not _LABEL_RE.fullmatch(v):
            raise ValueError("workdir must be a DNS-1123 label")
        return v

    @field_validator("pvc_size")
    @classmethod
    def _pvc_size(cls, v: str | None) -> str | None:
        if v is not None:
            _quantity(v)
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        bad = []
        for key in v:
            if (
                not _ENV_KEY_RE.fullmatch(key)
                or key == "POD_NAME"
                or key.startswith(_RESERVED_ENV_PREFIXES)
                or key.endswith(_RESERVED_ENV_SUFFIXES)
            ):
                bad.append(key)
        if bad:
            raise ValueError(
                "env keys must match [A-Z_][A-Z0-9_]* and must not be POD_NAME, CCW_*, CLAUDE_*, "
                "ANTHROPIC_*, *_PROXY or *_BASE_URL: " + ", ".join(sorted(bad))
            )
        return v

    @field_validator("discard_hook_url")
    @classmethod
    def _hook_url(cls, v: str | None) -> str | None:
        if v is not None:
            parsed = urlparse(v)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("discard_hook_url must be an http or https URL")
        return v


def fill_resource_defaults(spec: ClusterSpec, settings: Settings) -> ClusterSpec:
    """Fill each empty resources key from the settings defaults; explicit values win per key.

    A default limit is skipped when below the explicit request of the same resource, and a
    default request is skipped when above the explicit limit.
    """
    current = spec.resources or Resources()
    requests, limits = dict(current.requests), dict(current.limits)
    defaults = settings.default_resources()
    for key in ("cpu", "memory"):
        if key not in limits:
            default = defaults["limits"][key]
            if key not in requests or _not_above(requests[key], default):
                limits[key] = default
        if key not in requests:
            default = defaults["requests"][key]
            if key not in limits or _not_above(default, limits[key]):
                requests[key] = default
    return spec.model_copy(update={"resources": Resources(requests=requests, limits=limits)})


def _not_above(low: str, high: str) -> bool:
    try:
        return parse_quantity(low) <= parse_quantity(high)
    except ValueError:
        return True


def validate_spec(spec: ClusterSpec, settings: Settings, stored: ClusterSpec | None = None) -> None:
    """Validate a spec.

    `stored` stays for the preset PUT: a model, effort or image that has since left the settings
    lists is accepted only when unchanged from the stored preset spec. Scheduling a cluster passes
    no `stored` and validates fully.
    """
    from any_mcp.config import AnyMcpConfig
    from pydantic import ValidationError

    errors: list[str] = []
    if (
        spec.image is not None
        and spec.image not in settings.image_allowlist()
        and (stored is None or spec.image != stored.image)
    ):
        errors.append(f"image {spec.image!r} is not in the allowed agent images")
    if spec.discard_hook_url is not None:
        parsed = urlparse(spec.discard_hook_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append("discard_hook_url must be an http or https URL")
    if (
        spec.model is not None
        and spec.model not in settings.models
        and (stored is None or spec.model != stored.model)
    ):
        errors.append(f"model {spec.model!r} is not one of: {', '.join(settings.models)}")
    if (
        spec.effort
        and spec.effort not in settings.efforts
        and (stored is None or spec.effort != stored.effort)
    ):
        errors.append(f"effort {spec.effort!r} is not one of: {', '.join(settings.efforts)}")
    if spec.resources is not None:
        for key in ("cpu", "memory"):
            request = spec.resources.requests.get(key)
            limit = spec.resources.limits.get(key)
            if request is None or limit is None:
                continue
            if parse_quantity(request) > parse_quantity(limit):
                errors.append(f"resources: {key} request {request} exceeds limit {limit}")
    if spec.replicas > settings.max_replicas:
        errors.append(f"replicas {spec.replicas} exceeds the maximum {settings.max_replicas}")
    if spec.max_replicas is not None and spec.max_replicas > settings.max_replicas:
        errors.append(
            f"max_replicas {spec.max_replicas} exceeds the maximum {settings.max_replicas}"
        )
    if spec.pvc_size is not None:
        try:
            too_big = parse_quantity(spec.pvc_size) > parse_quantity(settings.max_pvc_size)
        except ValueError as exc:
            errors.append(f"pvc_size: {exc}")
        else:
            if too_big:
                errors.append(
                    f"pvc_size {spec.pvc_size} exceeds the maximum {settings.max_pvc_size}"
                )
    if spec.any_mcp_config is not None:
        try:
            config = AnyMcpConfig.model_validate(spec.any_mcp_config)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                errors.append(f"any_mcp_config.{loc}: {err['msg']}".replace(".: ", ": "))
        else:
            for entry in config.plugins:
                if RESERVED_PLUGIN in (entry.name, entry.namespace):
                    errors.append(
                        f"any_mcp_config.plugins: {RESERVED_PLUGIN!r} is reserved for the "
                        f"rendered plugin (entry {entry.name!r})"
                    )
    if errors:
        raise SpecInvalid(errors)
