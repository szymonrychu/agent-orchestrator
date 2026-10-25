"""ClusterSpec hive vocabulary: max_replicas, static and the derived hive rule."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec, SpecInvalid, validate_spec


def settings(**kw) -> Settings:
    return Settings(database_url="postgresql://unused/db", **kw)


def test_max_replicas_defaults_to_replicas() -> None:
    assert ClusterSpec().max_replicas == 1
    assert ClusterSpec(replicas=3).max_replicas == 3
    # a stored spec without the key loads as max_replicas == replicas
    assert ClusterSpec.model_validate({"replicas": 4}).max_replicas == 4
    assert ClusterSpec(replicas=2, max_replicas=5).max_replicas == 5


def test_cap_bounds() -> None:
    with pytest.raises(ValidationError):
        ClusterSpec(replicas=3, max_replicas=2)
    with pytest.raises(ValidationError):
        ClusterSpec(replicas=1, max_replicas=17)
    with pytest.raises(ValidationError):
        ClusterSpec(replicas=1, max_replicas=0)
    with pytest.raises(SpecInvalid, match="max_replicas"):
        validate_spec(ClusterSpec(replicas=1, max_replicas=5), settings(max_replicas=4))
    validate_spec(ClusterSpec(replicas=1, max_replicas=4), settings(max_replicas=4))


def test_static_requires_fixed_size() -> None:
    with pytest.raises(ValidationError):
        ClusterSpec(replicas=1, max_replicas=3, static=True)
    assert ClusterSpec(replicas=3, max_replicas=3, static=True).static is True
    assert ClusterSpec(replicas=3, static=True).max_replicas == 3


def test_derived_properties() -> None:
    alone = ClusterSpec()
    assert (alone.hive, alone.alone, alone.scalable) == (False, True, False)
    grow = ClusterSpec(replicas=1, max_replicas=3)
    assert (grow.hive, grow.alone, grow.scalable) == (True, False, True)
    fixed = ClusterSpec(replicas=2, static=True)
    assert (fixed.hive, fixed.alone, fixed.scalable) == (True, False, False)
    assert ClusterSpec(static=True).scalable is False


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "orchestrator", "plugin": "x:Y"},
        {"name": "other", "plugin": "x:Y", "namespace": "orchestrator"},
    ],
)
def test_orchestrator_plugin_name_reserved(entry) -> None:
    spec = ClusterSpec(any_mcp_config={"plugins": [entry]})
    with pytest.raises(SpecInvalid, match="orchestrator"):
        validate_spec(spec, settings())
    ok = ClusterSpec(any_mcp_config={"plugins": [{"name": "fine", "plugin": "x:Y"}]})
    validate_spec(ok, settings())


def test_preset_stays_sparse() -> None:
    spec = ClusterSpec.model_validate({"replicas": 1})
    assert spec.max_replicas == 1
    assert spec.model_dump(mode="json", exclude_unset=True) == {"replicas": 1}
    both = ClusterSpec.model_validate({"max_replicas": 3, "static": False})
    assert both.model_dump(mode="json", exclude_unset=True) == {
        "max_replicas": 3,
        "static": False,
    }


def test_cap_stable_after_backfill() -> None:
    stored = {"replicas": 2, "max_replicas": 2}
    spec = ClusterSpec.model_validate(stored)
    edited = ClusterSpec.model_validate({**stored, "replicas": 1})
    assert spec.max_replicas == 2
    assert edited.max_replicas == 2
    assert edited.hive is True
    with pytest.raises(ValidationError):
        ClusterSpec.model_validate({**stored, "replicas": 3})


@pytest.mark.parametrize("key", ["ORCH_AGENT_TOKEN", "ORCH_AGENT_URL", "ORCH_CLUSTER_ID", "ORCH_X"])
def test_orch_env_reserved(key: str) -> None:
    with pytest.raises(ValidationError):
        ClusterSpec(env={key: "v"})
