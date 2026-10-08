"""Offline tests for the vendor-neutral model role abstraction (AIDLC-46)."""

from __future__ import annotations

import ast
import asyncio
import re
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import ai_dlc.application.agent_harness as harness
from ai_dlc.application.agent_harness import (
    AgentContext,
    CapabilityRequirements,
    ContextRequirements,
    CostRequirements,
    DataClassification,
    DataGovernance,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    LatencyRequirements,
    ModelCapability,
    ModelProvider,
    ModelRole,
    ModelSelectionRequest,
    ReasoningLevel,
    RoleProfiles,
)
from ai_dlc.application.agent_harness import model_roles as module
from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.domain.identity import InitiativeMembership, Principal

MODULE_PATH = Path(module.__file__)
GOV = DataGovernance(allowed_classifications=frozenset(DataClassification))


def make_context() -> AgentContext:
    principal = Principal("user-1", "test")
    auth = ResolvedAuthorizationContext(
        principal, "initiative-1", InitiativeMembership("user-1", "initiative-1")
    )
    return AgentContext(
        request_id="request-1",
        correlation_id="correlation-1",
        session_id="session-1",
        trace_id="trace-1",
        authorization=auth,
    )


def profile(role: ModelRole = ModelRole.STANDARD_REASONING):
    return RoleProfiles.defaults().for_role(role)


def request(**kwargs):
    kwargs.setdefault("role", ModelRole.STANDARD_REASONING)
    kwargs.setdefault("data_classification", DataClassification.INTERNAL)
    return ModelSelectionRequest(**kwargs)


def test_four_roles_with_stable_values() -> None:
    assert [r.value for r in ModelRole] == [
        "routing",
        "standard_reasoning",
        "deep_reasoning",
        "independent_reviewer",
    ]
    profiles = RoleProfiles.defaults()
    assert set(profiles.profiles) == set(ModelRole)
    for role in ModelRole:
        assert profiles.for_role(role).role is role


def test_default_profiles_reflect_role_intent() -> None:
    routing, standard, deep, reviewer = (profile(r) for r in ModelRole)
    assert routing.capabilities.structured_output
    assert routing.latency.preference.value == "lowest"
    assert routing.cost.sensitivity.value == "high"
    assert deep.capabilities.reasoning == ReasoningLevel.EXTENDED
    assert deep.capabilities.tool_calling
    assert deep.context.min_context_tokens > standard.context.min_context_tokens
    assert standard.context.min_context_tokens > routing.context.min_context_tokens
    assert reviewer.require_separate_from_generator
    assert not any(p.require_separate_from_generator for p in (routing, standard, deep))
    assert reviewer.capabilities.structured_output


def test_no_vendor_or_model_names_in_module_source() -> None:
    source = MODULE_PATH.read_text().lower()
    forbidden = (
        "anthropic",
        "claude",
        "openai",
        "gpt",
        "bedrock",
        "titan",
        "llama",
        "gemini",
        "mistral",
        "arn:",
        "amazonaws",
        "api_key",
    )
    for word in forbidden:
        assert not re.search(rf"\b{re.escape(word)}", source), word
    for role in ModelRole:
        assert not any(word in role.value for word in forbidden)


def test_profiles_have_no_model_identifier_fields() -> None:
    for cls in (
        module.ModelRequirements,
        module.CapabilityRequirements,
        module.ContextRequirements,
        module.LatencyRequirements,
        module.CostRequirements,
        module.DataGovernance,
        module.ModelSelectionRequest,
    ):
        assert not any("model_id" in f or "arn" in f.split("_") for f in cls.model_fields), cls


def test_capability_requirements_validation() -> None:
    with pytest.raises(ValidationError):
        CapabilityRequirements(input_modalities=frozenset())
    with pytest.raises(ValidationError):
        CapabilityRequirements(reasoning="genius")
    with pytest.raises(ValidationError):
        CapabilityRequirements(unknown=True)


def test_context_requirements_and_model_capability_compat() -> None:
    req = ContextRequirements(min_context_tokens=32_000, min_output_tokens=4_096)
    ok = ModelCapability(model_id="cfg-a", max_context_tokens=64_000, reserved_output_tokens=4_096)
    small = ModelCapability(model_id="cfg-b", max_context_tokens=8_000, reserved_output_tokens=512)
    assert req.is_satisfied_by(ok)
    assert not req.is_satisfied_by(small)
    for bad in ((0, 1), (10, 0), (100, 100), (100, 200)):
        with pytest.raises(ValidationError):
            ContextRequirements(min_context_tokens=bad[0], min_output_tokens=bad[1])


def test_latency_constraints() -> None:
    assert LatencyRequirements(max_latency_ms=1000).max_latency_ms == 1000
    for bad in (0, -5, 10**9):
        with pytest.raises(ValidationError):
            LatencyRequirements(max_latency_ms=bad)
    assert LatencyRequirements(deadline_required=True).deadline_required


def test_cost_constraints() -> None:
    cost = CostRequirements(max_input_cost_per_million_tokens=Decimal("1.5"))
    assert cost.require_pricing_metadata
    with pytest.raises(ValidationError):
        CostRequirements(max_output_cost_per_million_tokens=Decimal("-1"))
    with pytest.raises(ValidationError):
        CostRequirements(max_output_cost_per_million_tokens=Decimal("NaN"))
    with pytest.raises(ValidationError):
        CostRequirements(currency="usd")
    with pytest.raises(ValidationError):
        CostRequirements(
            max_input_cost_per_million_tokens=Decimal("1"), require_pricing_metadata=False
        )


def test_governance_validation() -> None:
    with pytest.raises(ValidationError):
        DataGovernance(allowed_classifications=frozenset())
    with pytest.raises(ValidationError):
        DataGovernance(
            allowed_classifications=GOV.allowed_classifications,
            allowed_deployment_types=frozenset(),
        )
    with pytest.raises(ValidationError):
        DataGovernance(
            allowed_classifications=GOV.allowed_classifications,
            allowed_providers=frozenset({"a"}),
            denied_providers=frozenset({"a"}),
        )
    with pytest.raises(ValidationError):
        DataGovernance(
            allowed_classifications=GOV.allowed_classifications,
            allowed_regions=frozenset({"r1"}),
            denied_regions=frozenset({"r1"}),
        )
    with pytest.raises(ValidationError):
        DataGovernance(
            allowed_classifications=GOV.allowed_classifications,
            allowed_regions=frozenset({"bad region!"}),
        )


def test_objects_are_immutable() -> None:
    p = profile()
    with pytest.raises(ValidationError):
        p.role = ModelRole.ROUTING  # type: ignore[misc]
    with pytest.raises(ValidationError):
        p.context.min_context_tokens = 1  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        p.governance.allowed_regions.add("x")  # type: ignore[attr-defined]


def test_profiles_from_configuration_and_validation() -> None:
    config = {role: profile(role).model_dump(mode="json") for role in ModelRole}
    config[ModelRole.ROUTING]["context"]["min_context_tokens"] = 4000
    loaded = RoleProfiles.model_validate({"profiles": config})
    assert loaded.for_role(ModelRole.ROUTING).context.min_context_tokens == 4000
    missing = {k: v for k, v in config.items() if k != ModelRole.DEEP_REASONING}
    with pytest.raises(ValidationError):
        RoleProfiles.model_validate({"profiles": missing})
    mismatched = dict(config)
    mismatched[ModelRole.ROUTING] = config[ModelRole.DEEP_REASONING]
    with pytest.raises(ValidationError):
        RoleProfiles.model_validate({"profiles": mismatched})


def test_request_tightens_but_never_relaxes() -> None:
    p = profile(ModelRole.ROUTING)
    eff = request(
        role=ModelRole.ROUTING,
        requested_context_tokens=100_000,
        requested_output_tokens=1,
        max_latency_ms=10_000,
        required_capabilities=CapabilityRequirements(tool_calling=True),
    ).effective_requirements(p)
    assert eff.context.min_context_tokens == 100_000
    assert eff.context.min_output_tokens == p.context.min_output_tokens
    assert eff.latency.max_latency_ms == p.latency.max_latency_ms  # min(5000, 10000)
    assert eff.capabilities.tool_calling and eff.capabilities.structured_output
    tighter = request(role=ModelRole.ROUTING, max_latency_ms=100).effective_requirements(p)
    assert tighter.latency.max_latency_ms == 100


def test_request_cost_constraints_merge() -> None:
    base = profile().model_copy(
        update={"cost": CostRequirements(max_input_cost_per_million_tokens=Decimal("5"))}
    )
    eff = request(
        max_input_cost_per_million_tokens=Decimal("9"),
        max_output_cost_per_million_tokens=Decimal("3"),
    ).effective_requirements(base)
    assert eff.cost.max_input_cost_per_million_tokens == Decimal("5")
    assert eff.cost.max_output_cost_per_million_tokens == Decimal("3")
    with pytest.raises(ValueError):
        request(max_input_cost_per_million_tokens=Decimal("-1")).effective_requirements(base)


def test_data_classification_restriction() -> None:
    restricted = profile().model_copy(
        update={
            "governance": DataGovernance(
                allowed_classifications=frozenset({DataClassification.PUBLIC})
            )
        }
    )
    with pytest.raises(ValueError, match="classification"):
        request(data_classification=DataClassification.RESTRICTED).effective_requirements(
            restricted
        )
    request(data_classification=DataClassification.PUBLIC).effective_requirements(restricted)


def test_regional_and_provider_restrictions_only_narrow() -> None:
    base = profile().model_copy(
        update={
            "governance": DataGovernance(
                allowed_classifications=frozenset(DataClassification),
                allowed_regions=frozenset({"region-a", "region-b"}),
                allowed_providers=frozenset({"prov-1", "prov-2"}),
            )
        }
    )
    eff = request(
        allowed_regions=frozenset({"region-b", "region-z"}),
        denied_providers=frozenset({"prov-2"}),
    ).effective_requirements(base)
    assert eff.governance.allowed_regions == {"region-b"}
    assert eff.governance.allowed_providers == {"prov-1"}
    assert eff.governance.denied_providers == {"prov-2"}
    with pytest.raises(ValueError, match="regions"):
        request(allowed_regions=frozenset({"region-z"})).effective_requirements(base)
    with pytest.raises(ValueError, match="providers"):
        request(allowed_providers=frozenset({"prov-9"})).effective_requirements(base)
    with pytest.raises(ValueError, match="eligible"):
        request(denied_providers=frozenset({"prov-1", "prov-2"})).effective_requirements(base)


def test_request_validation_and_role_mismatch() -> None:
    for bad in (
        {"requested_context_tokens": 0},
        {"allowed_regions": frozenset({"x y"})},
        {"bogus": 1},
    ):
        with pytest.raises(ValidationError):
            request(**bad)
    with pytest.raises(ValidationError):
        ModelSelectionRequest(role="gpt-x", data_classification="internal")
    with pytest.raises(ValueError, match="role"):
        request(role=ModelRole.ROUTING).effective_requirements(profile(ModelRole.DEEP_REASONING))


def test_reviewer_separation_is_declared_not_selected() -> None:
    assert "separate_from_deployments" in ModelSelectionRequest.model_fields
    eff = request(
        role=ModelRole.INDEPENDENT_REVIEWER, separate_from_deployments=frozenset({"gen-1"})
    ).effective_requirements(profile(ModelRole.INDEPENDENT_REVIEWER))
    assert eff.require_separate_from_generator


class RecordingProvider:
    def __init__(self) -> None:
        self.roles: list[str] = []

    async def invoke_model(self, role: str, request: Invocation, *, context: AgentContext):
        self.roles.append(role)
        return ExecutionResult(
            status=ExecutionStatus.SUCCEEDED,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
        )


def test_model_provider_accepts_roles_unchanged() -> None:
    provider: ModelProvider = RecordingProvider()
    ctx = make_context()
    for role in ModelRole:
        asyncio.run(provider.invoke_model(role, Invocation(input={}), context=ctx))
    assert provider.roles == [r.value for r in ModelRole]
    assert not hasattr(module, "ModelProvider")  # no competing interface


def test_selection_does_not_touch_agent_context_or_authorization() -> None:
    ctx = make_context()
    before = repr(ctx)
    for role in ModelRole:
        request(role=role).effective_requirements(profile(role))
    assert repr(ctx) == before
    fields = set()
    for cls in (module.ModelRequirements, module.ModelSelectionRequest, module.RoleProfiles):
        fields |= set(cls.model_fields)
    assert not fields & {"scopes", "permissions", "principal", "initiative_id", "tools", "grants"}


def test_module_has_no_authorization_or_selection_logic() -> None:
    tree = ast.parse(MODULE_PATH.read_text())
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any("authorization" in m or "gateway" in m or "adapters" in m for m in imported)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not {"select_model", "route", "select", "fallback"} & funcs


def test_public_exports_and_version() -> None:
    from ai_dlc.application.agent_harness import INTERFACE_VERSION

    assert INTERFACE_VERSION == "1.5.0"
    for name in (
        "ModelRole",
        "ModelRequirements",
        "ModelSelectionRequest",
        "RoleProfiles",
        "CapabilityRequirements",
        "ContextRequirements",
        "LatencyRequirements",
        "CostRequirements",
        "DataGovernance",
        "DataClassification",
    ):
        assert name in harness.__all__
        assert getattr(harness, name) is getattr(module, name)


def _governed(**kwargs):
    return profile().model_copy(
        update={
            "governance": DataGovernance(
                allowed_classifications=frozenset({DataClassification.INTERNAL}), **kwargs
            )
        }
    )


def test_provider_allow_deny_conflict_fails_closed() -> None:
    base = _governed(allowed_providers=frozenset({"p1", "p2"}))
    with pytest.raises(ValueError, match="eligible"):
        request(
            allowed_providers=frozenset({"p1"}), denied_providers=frozenset({"p1"})
        ).effective_requirements(base)
    open_base = _governed()
    with pytest.raises(ValueError, match="eligible"):
        request(
            allowed_providers=frozenset({"p1"}), denied_providers=frozenset({"p1"})
        ).effective_requirements(open_base)


def test_region_allow_deny_conflict_fails_closed() -> None:
    base = _governed(denied_regions=frozenset({"r-bad"}))
    with pytest.raises(ValueError, match="eligible"):
        request(allowed_regions=frozenset({"r-bad"})).effective_requirements(base)
    eff = request(allowed_regions=frozenset({"r-bad", "r-ok"})).effective_requirements(base)
    assert eff.governance.allowed_regions == {"r-ok"}
    assert not eff.governance.allowed_regions & eff.governance.denied_regions


def test_no_eligible_provider_left() -> None:
    base = _governed(allowed_providers=frozenset({"p1"}))
    with pytest.raises(ValueError):
        request(denied_providers=frozenset({"p1"})).effective_requirements(base)
    with pytest.raises(ValueError):
        request(allowed_providers=frozenset({"p2"})).effective_requirements(base)


def test_no_eligible_region_left() -> None:
    base = _governed(allowed_regions=frozenset({"r1"}))
    with pytest.raises(ValueError):
        request(allowed_regions=frozenset({"r2"})).effective_requirements(base)


def test_existing_governance_restrictions_remain_intact() -> None:
    base = _governed(
        allowed_providers=frozenset({"p1", "p2"}),
        denied_providers=frozenset({"p9"}),
        allowed_regions=frozenset({"r1", "r2"}),
        denied_regions=frozenset({"r9"}),
        allowed_deployment_types=frozenset({module.DeploymentType.PRIVATE_ENDPOINT}),
    )
    eff = request(denied_providers=frozenset({"p2"})).effective_requirements(base)
    gov = eff.governance
    assert gov.allowed_classifications == {DataClassification.INTERNAL}
    assert gov.allowed_deployment_types == {module.DeploymentType.PRIVATE_ENDPOINT}
    assert gov.allowed_providers == {"p1"}
    assert gov.denied_providers == {"p2", "p9"}
    assert gov.allowed_regions == {"r1", "r2"}
    assert gov.denied_regions == {"r9"}
    assert isinstance(eff, module.ModelRequirements)
    # the result round-trips through validation
    assert module.ModelRequirements.model_validate(eff.model_dump()) == eff
