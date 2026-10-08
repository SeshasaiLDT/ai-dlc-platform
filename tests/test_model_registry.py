"""Offline tests for the governed model registry (AIDLC-47)."""

from __future__ import annotations

import ast
import asyncio
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import ai_dlc.application.model_registry as registry_pkg
from ai_dlc.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    InMemoryMembershipRepository,
    InMemoryPlatformAdminRepository,
)
from ai_dlc.adapters.model_registry import InMemoryModelRegistryRepository
from ai_dlc.application.agent_harness import (
    AgentContext,
    CapabilityRequirements,
    DataClassification,
    DataGovernance,
    DeploymentType,
    ExecutionResult,
    ExecutionStatus,
    InputModality,
    Invocation,
    LatencyPreference,
    ModelCapability,
    ModelProvider,
    ModelRole,
    ModelSelectionRequest,
    ReasoningLevel,
    ResponseCapability,
    RoleProfiles,
)
from ai_dlc.application.authorization import (
    AuthorizationDeniedError,
    AuthorizationService,
    RoleGrant,
    RolePolicy,
)
from ai_dlc.application.model_registry import (
    DeploymentCapabilities,
    DeploymentContext,
    DeploymentExistsError,
    DeploymentGovernance,
    DeploymentNotFoundError,
    IneligibleReason,
    LatencyMetadata,
    ModelDeploymentSpec,
    ModelRegistryAdmin,
    ModelRegistryAuditError,
    ModelRegistryReader,
    OperationalAvailability,
    PricingMetadata,
    RegistryOperation,
    RevisionConflictError,
)
from ai_dlc.domain.identity import AdminPermission, Principal, Role

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
ADMIN = Principal("admin-1", "test")
USER = Principal("user-1", "test")
ALL = frozenset(DataClassification)


def authorizer() -> AuthorizationService:
    policy = RolePolicy(
        (
            RoleGrant(
                Role.PLATFORM_ADMIN, admin_permissions=frozenset({AdminPermission.PLATFORM_MANAGE})
            ),
        )
    )
    return AuthorizationService(
        InMemoryMembershipRepository(),
        policy,
        InMemoryAuthorizationAuditSink(),
        platform_admins=InMemoryPlatformAdminRepository(frozenset({ADMIN.subject_id})),
        clock=lambda: NOW,
    )


def spec(deployment_id: str = "dep-a", **overrides) -> ModelDeploymentSpec:
    values = dict(
        deployment_id=deployment_id,
        provider_id="provider-one",
        model_identifier="model-x.v1",
        region="region-a",
        deployment_type=DeploymentType.MANAGED_SERVICE,
        capabilities=DeploymentCapabilities(
            structured_output=True,
            tool_calling=True,
            reasoning=ReasoningLevel.EXTENDED,
            input_modalities=frozenset({InputModality.TEXT, InputModality.IMAGE}),
            response_capabilities=frozenset({ResponseCapability.STREAMING}),
        ),
        context=DeploymentContext(max_context_tokens=200_000, max_output_tokens=32_000),
        latency=LatencyMetadata(
            typical_latency_ms=2000, performance_class=LatencyPreference.BALANCED
        ),
        pricing=PricingMetadata(
            input_cost_per_million_tokens=Decimal("3"), output_cost_per_million_tokens=Decimal("15")
        ),
        governance=DeploymentGovernance(
            supported_classifications=ALL, eligible_roles=frozenset(ModelRole)
        ),
    )
    values.update(overrides)
    return ModelDeploymentSpec(**values)


class Env:
    def __init__(self, **repo_kwargs) -> None:
        self.repo = InMemoryModelRegistryRepository(**repo_kwargs)
        self.admin = ModelRegistryAdmin(self.repo, authorizer(), clock=lambda: NOW)
        self.reader = ModelRegistryReader(self.repo)

    def add(self, deployment_id="dep-a", *, enable=True, **overrides):
        record = self.admin.register(ADMIN, spec(deployment_id, **overrides), correlation_id="c-1")
        if enable:
            record = self.admin.enable(
                ADMIN, deployment_id, expected_revision=record.revision, correlation_id="c-2"
            )
        return record


def sel(role=ModelRole.STANDARD_REASONING, **kwargs) -> ModelSelectionRequest:
    kwargs.setdefault("data_classification", DataClassification.INTERNAL)
    return ModelSelectionRequest(role=role, **kwargs)


PROFILES = RoleProfiles.defaults()


def eligible_ids(env: Env, request: ModelSelectionRequest, profiles=PROFILES) -> list[str]:
    return [m.deployment_id for m in env.reader.query_eligible(request, profiles)]


def reasons(env: Env, request: ModelSelectionRequest, profiles=PROFILES) -> set[IneligibleReason]:
    return {r for res in env.reader.evaluate(request, profiles) for r in res.reasons}


# --- record validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"deployment_id": "Bad_ID"},
        {"deployment_id": "x" * 100},
        {"provider_id": "Provider One"},
        {"model_identifier": "arn:aws:bedrock:us:123:model/x"},
        {"model_identifier": "https://host/model"},
        {"model_identifier": "key with space"},
        {"inference_profile_id": "arn:aws:x"},
        {"region": "Region A!"},
        {"unknown_field": 1},
        {"deployment_type": "quantum"},
    ],
)
def test_spec_validation_rejects_bad_input(overrides) -> None:
    with pytest.raises(ValidationError):
        spec(**overrides)


def test_nested_validation_and_unknown_capabilities_fail_closed() -> None:
    with pytest.raises(ValidationError):
        DeploymentContext(max_context_tokens=100, max_output_tokens=100)
    with pytest.raises(ValidationError):
        DeploymentCapabilities(input_modalities=frozenset({"telepathy"}))
    with pytest.raises(ValidationError):
        DeploymentCapabilities(reasoning="superhuman")
    with pytest.raises(ValidationError):
        DeploymentCapabilities(input_modalities=frozenset())
    with pytest.raises(ValidationError):
        PricingMetadata(input_cost_per_million_tokens=Decimal("-1"))
    with pytest.raises(ValidationError):
        DeploymentGovernance(
            supported_classifications=frozenset(), eligible_roles=frozenset(ModelRole)
        )
    with pytest.raises(ValidationError):
        DeploymentGovernance(supported_classifications=ALL, eligible_roles=frozenset())


def test_records_are_immutable_and_hold_no_secrets() -> None:
    s = spec()
    with pytest.raises(ValidationError):
        s.region = "elsewhere"  # type: ignore[misc]
    names = set(ModelDeploymentSpec.model_fields) | set(DeploymentContext.model_fields)
    assert not any(
        w in n
        for n in names
        for w in ("secret", "token_value", "password", "api_key", "credential")
    )


def test_spec_adapts_to_model_capability() -> None:
    cap = spec().to_model_capability(reserved_output_tokens=4096)
    assert isinstance(cap, ModelCapability) and cap.max_context_tokens == 200_000
    with pytest.raises(ValueError):
        spec().to_model_capability(reserved_output_tokens=10**6)


# --- register / read / list ------------------------------------------------------------------


def test_register_read_list_and_default_disabled() -> None:
    env = Env()
    record = env.admin.register(ADMIN, spec("dep-b"), correlation_id="c-1")
    env.admin.register(ADMIN, spec("dep-a"), correlation_id="c-1")
    assert not record.enabled and record.revision == 1
    assert env.reader.get("dep-b") == record
    assert [m.deployment_id for m in env.reader.list()] == ["dep-a", "dep-b"]
    with pytest.raises(DeploymentExistsError):
        env.admin.register(ADMIN, spec("dep-a"), correlation_id="c-1")
    with pytest.raises(DeploymentNotFoundError):
        env.reader.get("missing")


# --- enable / disable / dynamic refresh ------------------------------------------------------


def test_enable_disable_and_dynamic_refresh_without_new_reader() -> None:
    env = Env()
    rec = env.add(enable=False)
    request = sel()
    assert eligible_ids(env, request) == []
    assert IneligibleReason.DISABLED in reasons(env, request)
    rec = env.admin.enable(ADMIN, "dep-a", expected_revision=rec.revision, correlation_id="c")
    assert eligible_ids(env, request) == ["dep-a"]
    assert env.reader.get("dep-a").revision == rec.revision
    rec = env.admin.disable(ADMIN, "dep-a", expected_revision=rec.revision, correlation_id="c")
    assert eligible_ids(env, request) == []
    assert [m.deployment_id for m in env.reader.list(enabled_only=True)] == []
    assert rec.revision == 3


def test_enable_disable_are_idempotent_without_revision_or_audit() -> None:
    env = Env()
    rec = env.add()
    n = len(env.repo.audit_events())
    again = env.admin.enable(ADMIN, "dep-a", expected_revision=rec.revision, correlation_id="c")
    assert again == rec and len(env.repo.audit_events()) == n


def test_availability_is_separate_from_enablement() -> None:
    env = Env()
    rec = env.add()
    rec = env.admin.set_availability(
        ADMIN,
        "dep-a",
        OperationalAvailability.UNAVAILABLE,
        expected_revision=rec.revision,
        correlation_id="c",
    )
    assert rec.enabled
    assert eligible_ids(env, sel()) == []
    assert IneligibleReason.UNAVAILABLE in reasons(env, sel())
    rec = env.admin.set_availability(
        ADMIN,
        "dep-a",
        OperationalAvailability.DEGRADED,
        expected_revision=rec.revision,
        correlation_id="c",
    )
    assert eligible_ids(env, sel()) == ["dep-a"]


def test_update_metadata_changes_fields_and_noop_is_unaudited() -> None:
    env = Env()
    rec = env.add()
    new = spec(region="region-b")
    updated = env.admin.update_metadata(
        ADMIN, new, expected_revision=rec.revision, correlation_id="c"
    )
    assert (
        updated.spec.region == "region-b"
        and updated.enabled
        and updated.revision == rec.revision + 1
    )
    event = env.repo.audit_events("dep-a")[-1]
    assert event.operation is RegistryOperation.UPDATE_METADATA and event.changed_fields == (
        "region",
    )
    n = len(env.repo.audit_events())
    same = env.admin.update_metadata(
        ADMIN, new, expected_revision=updated.revision, correlation_id="c"
    )
    assert same == updated and len(env.repo.audit_events()) == n
    with pytest.raises(DeploymentNotFoundError):
        env.admin.update_metadata(ADMIN, spec("nope"), expected_revision=1, correlation_id="c")


# --- eligibility -----------------------------------------------------------------------------


def test_declared_role_is_necessary_but_not_sufficient() -> None:
    gov = DeploymentGovernance(
        supported_classifications=ALL, eligible_roles=frozenset({ModelRole.ROUTING})
    )
    small = spec(
        governance=gov,
        context=DeploymentContext(max_context_tokens=4000, max_output_tokens=500),
    )
    env = Env()
    env.admin.register(ADMIN, small, correlation_id="c")
    env.admin.enable(ADMIN, "dep-a", expected_revision=1, correlation_id="c")
    # declares routing but fails routing's default context requirement
    assert eligible_ids(env, sel(ModelRole.ROUTING)) == []
    assert IneligibleReason.CONTEXT_WINDOW in reasons(env, sel(ModelRole.ROUTING))
    # and is not declared for other roles
    assert IneligibleReason.ROLE_NOT_DECLARED in reasons(env, sel(ModelRole.STANDARD_REASONING))


def test_capability_matching() -> None:
    env = Env()
    env.add(
        capabilities=DeploymentCapabilities(structured_output=False, reasoning=ReasoningLevel.BASIC)
    )
    r = reasons(
        env,
        sel(
            required_capabilities=CapabilityRequirements(
                tool_calling=True,
                input_modalities=frozenset({InputModality.AUDIO}),
                response_capabilities=frozenset({ResponseCapability.CITATIONS}),
            )
        ),
    )
    assert {
        IneligibleReason.STRUCTURED_OUTPUT,
        IneligibleReason.TOOL_CALLING,
        IneligibleReason.REASONING_LEVEL,
        IneligibleReason.INPUT_MODALITY,
        IneligibleReason.RESPONSE_CAPABILITY,
    } <= r


def test_context_and_output_limits() -> None:
    env = Env()
    env.add()
    assert eligible_ids(
        env, sel(requested_context_tokens=200_000, requested_output_tokens=32_000)
    ) == ["dep-a"]
    assert IneligibleReason.CONTEXT_WINDOW in reasons(env, sel(requested_context_tokens=200_001))
    assert IneligibleReason.OUTPUT_CAPACITY in reasons(
        env, sel(requested_context_tokens=100_000, requested_output_tokens=32_001)
    )


def test_data_classification_restrictions() -> None:
    env = Env()
    env.add(
        governance=DeploymentGovernance(
            supported_classifications=frozenset({DataClassification.PUBLIC}),
            eligible_roles=frozenset(ModelRole),
        )
    )
    assert eligible_ids(env, sel(data_classification=DataClassification.PUBLIC)) == ["dep-a"]
    request = sel(data_classification=DataClassification.RESTRICTED)
    assert eligible_ids(env, request) == []
    assert IneligibleReason.CLASSIFICATION in reasons(env, request)


def test_provider_region_and_deployment_type_restrictions() -> None:
    env = Env()
    env.add()
    assert eligible_ids(env, sel(allowed_providers=frozenset({"provider-one"}))) == ["dep-a"]
    assert IneligibleReason.PROVIDER in reasons(env, sel(allowed_providers=frozenset({"other"})))
    assert IneligibleReason.PROVIDER in reasons(
        env, sel(denied_providers=frozenset({"provider-one"}))
    )
    assert IneligibleReason.REGION in reasons(env, sel(allowed_regions=frozenset({"region-z"})))
    assert eligible_ids(env, sel(allowed_regions=frozenset({"region-a"}))) == ["dep-a"]
    only_private = RoleProfiles.defaults()
    standard = only_private.for_role(ModelRole.STANDARD_REASONING)
    restricted = standard.model_copy(
        update={
            "governance": DataGovernance(
                allowed_classifications=ALL,
                allowed_deployment_types=frozenset({DeploymentType.PRIVATE_ENDPOINT}),
                denied_regions=frozenset({"region-a"}),
            )
        }
    )
    profiles = RoleProfiles(
        profiles={**only_private.profiles, ModelRole.STANDARD_REASONING: restricted}
    )
    found = reasons(env, sel(), profiles)
    assert {IneligibleReason.DEPLOYMENT_TYPE, IneligibleReason.REGION} <= found


def test_user_provider_preference_cannot_override_org_restrictions() -> None:
    env = Env()
    env.add()
    standard = PROFILES.for_role(ModelRole.STANDARD_REASONING)
    org = standard.model_copy(
        update={
            "governance": DataGovernance(
                allowed_classifications=ALL, denied_providers=frozenset({"provider-one"})
            )
        }
    )
    profiles = RoleProfiles(profiles={**PROFILES.profiles, ModelRole.STANDARD_REASONING: org})
    # a request asking for the denied provider fails closed rather than widening the profile
    with pytest.raises(ValueError):
        env.reader.query_eligible(sel(allowed_providers=frozenset({"provider-one"})), profiles)
    assert eligible_ids(env, sel(), profiles) == []


def test_cost_ceilings_and_pricing_metadata() -> None:
    env = Env()
    env.add()
    assert eligible_ids(env, sel(max_input_cost_per_million_tokens=Decimal("3"))) == ["dep-a"]
    assert IneligibleReason.COST_CEILING in reasons(
        env, sel(max_output_cost_per_million_tokens=Decimal("10"))
    )
    env2 = Env()
    env2.add(pricing=PricingMetadata())
    assert IneligibleReason.PRICING_MISSING in reasons(env2, sel())
    env3 = Env()
    env3.add(
        pricing=PricingMetadata(
            input_cost_per_million_tokens=Decimal("1"),
            output_cost_per_million_tokens=Decimal("1"),
            currency="EUR",
        )
    )
    assert IneligibleReason.CURRENCY_MISMATCH in reasons(
        env3, sel(max_input_cost_per_million_tokens=Decimal("5"))
    )


def test_latency_constraints_fail_closed_when_unknown() -> None:
    env = Env()
    env.add()
    assert eligible_ids(env, sel(max_latency_ms=2000)) == ["dep-a"]
    assert IneligibleReason.LATENCY_CEILING in reasons(env, sel(max_latency_ms=1999))
    env2 = Env()
    env2.add(latency=LatencyMetadata())
    assert IneligibleReason.LATENCY_UNKNOWN in reasons(env2, sel(max_latency_ms=5000))
    assert eligible_ids(env2, sel()) == ["dep-a"]  # no latency requirement, nothing to check


def test_reviewer_separation_from_generator() -> None:
    env = Env()
    env.add("dep-a")
    env.add("dep-b")
    request = sel(ModelRole.INDEPENDENT_REVIEWER, separate_from_deployments=frozenset({"dep-a"}))
    assert eligible_ids(env, request) == ["dep-b"]


def test_results_ordered_by_id_not_ranked() -> None:
    env = Env()
    for name in ("dep-c", "dep-a", "dep-b"):
        env.add(name)
    assert eligible_ids(env, sel()) == ["dep-a", "dep-b", "dep-c"]


# --- authorization ---------------------------------------------------------------------------


def test_unauthorized_mutations_are_rejected_and_change_nothing() -> None:
    env = Env()
    rec = env.add()
    before = env.repo.list_all()
    events = env.repo.audit_events()
    for call in (
        lambda: env.admin.register(USER, spec("dep-z"), correlation_id="c"),
        lambda: env.admin.enable(USER, "dep-a", expected_revision=rec.revision, correlation_id="c"),
        lambda: env.admin.disable(
            USER, "dep-a", expected_revision=rec.revision, correlation_id="c"
        ),
        lambda: env.admin.update_metadata(
            USER, spec(region="r2"), expected_revision=rec.revision, correlation_id="c"
        ),
    ):
        with pytest.raises(AuthorizationDeniedError):
            call()
    for bad in (None, "admin-1", {"subject_id": "admin-1"}):
        with pytest.raises(TypeError):
            env.admin.disable(bad, "dep-a", expected_revision=rec.revision, correlation_id="c")
    assert env.repo.list_all() == before and env.repo.audit_events() == events


def test_reader_has_no_administrative_surface() -> None:
    public = {n for n in dir(ModelRegistryReader) if not n.startswith("_")}
    assert public == {"get", "list", "evaluate", "query_eligible"}
    assert not hasattr(ModelRegistryReader(InMemoryModelRegistryRepository()), "commit")


def test_invalid_correlation_id_rejected() -> None:
    env = Env()
    with pytest.raises(ValueError):
        env.admin.register(ADMIN, spec(), correlation_id="bad id with spaces\n")


# --- concurrency and audit ------------------------------------------------------------------


def test_stale_revision_rejected_and_record_preserved() -> None:
    env = Env()
    rec = env.add()
    env.admin.disable(ADMIN, "dep-a", expected_revision=rec.revision, correlation_id="c")
    stored = env.reader.get("dep-a")
    with pytest.raises(RevisionConflictError):
        env.admin.enable(ADMIN, "dep-a", expected_revision=rec.revision, correlation_id="c")
    with pytest.raises(RevisionConflictError):
        env.admin.update_metadata(ADMIN, spec(region="r9"), expected_revision=1, correlation_id="c")
    assert env.reader.get("dep-a") == stored


def test_concurrent_updates_only_one_wins() -> None:
    env = Env()
    rec = env.add(enable=False)
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def work() -> None:
        barrier.wait()
        try:
            env.admin.enable(ADMIN, "dep-a", expected_revision=rec.revision, correlation_id="c")
            outcomes.append("ok")
        except RevisionConflictError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("ok") == 1 and outcomes.count("conflict") == 7
    assert env.reader.get("dep-a").revision == rec.revision + 1
    assert (
        len([e for e in env.repo.audit_events("dep-a") if e.operation is RegistryOperation.ENABLE])
        == 1
    )


def test_every_mutation_is_audited_with_required_fields() -> None:
    env = Env()
    rec = env.add()
    env.admin.disable(ADMIN, "dep-a", expected_revision=rec.revision, correlation_id="corr-9")
    events = env.repo.audit_events("dep-a")
    assert [e.operation for e in events] == [
        RegistryOperation.REGISTER,
        RegistryOperation.ENABLE,
        RegistryOperation.DISABLE,
    ]
    last = events[-1]
    assert (last.actor_id, last.previous_revision, last.new_revision) == ("admin-1", 2, 3)
    assert last.changed_fields == ("enabled",) and last.correlation_id == "corr-9"
    assert last.occurred_at == NOW and last.authorization_decision_id
    assert events[0].previous_revision is None and events[0].new_revision == 1


def test_audit_failure_prevents_mutation() -> None:
    def failing(_event) -> None:
        raise RuntimeError("audit store down: secret-token-123")

    env = Env(audit_hook=failing)
    with pytest.raises(ModelRegistryAuditError) as err:
        env.admin.register(ADMIN, spec(), correlation_id="c")
    assert "secret-token-123" not in str(err.value)
    assert env.repo.list_all() == () and env.repo.audit_events() == ()

    flaky = {"fail": False}

    def hook(_event) -> None:
        if flaky["fail"]:
            raise RuntimeError("down")

    env2 = Env(audit_hook=hook)
    rec = env2.add()
    flaky["fail"] = True
    with pytest.raises(ModelRegistryAuditError):
        env2.admin.disable(ADMIN, "dep-a", expected_revision=rec.revision, correlation_id="c")
    assert env2.reader.get("dep-a") == rec and len(env2.repo.audit_events()) == 2


def test_audit_events_contain_names_only() -> None:
    env = Env()
    rec = env.add()
    env.admin.update_metadata(
        ADMIN,
        spec(model_identifier="model-secretish-v2"),
        expected_revision=rec.revision,
        correlation_id="c",
    )
    text = repr(env.repo.audit_events())
    assert "model-secretish-v2" not in text and "provider-one" not in text
    assert {f for e in env.repo.audit_events() for f in e.__slots__} == {
        "deployment_id",
        "operation",
        "actor_id",
        "occurred_at",
        "previous_revision",
        "new_revision",
        "changed_fields",
        "correlation_id",
        "authorization_decision_id",
    }


# --- boundaries and compatibility ------------------------------------------------------------


def test_registry_records_grant_no_permissions() -> None:
    fields = set(ModelDeploymentSpec.model_fields) | set(registry_pkg.RegisteredModel.model_fields)
    assert not fields & {"scopes", "permissions", "tools", "grants", "principal", "roles_granted"}


def test_registry_is_not_imported_by_sdk_and_has_no_invocation_or_routing() -> None:
    harness = Path(registry_pkg.__file__).parents[1] / "agent_harness"
    for path in harness.glob("*.py"):
        assert "model_registry" not in path.read_text(), path.name
    pkg = Path(registry_pkg.__file__).parent
    names = {
        n.name
        for p in pkg.glob("*.py")
        for n in ast.walk(ast.parse(p.read_text()))
        if isinstance(n, ast.FunctionDef)
    }
    assert not names & {"select_model", "route", "invoke_model", "fallback", "rank"}
    for p in pkg.glob("*.py"):
        assert "ModelProvider" not in p.read_text()


def test_model_provider_interface_unchanged_and_accepts_roles() -> None:
    class Provider:
        async def invoke_model(self, role: str, request: Invocation, *, context: AgentContext):
            return ExecutionResult(
                status=ExecutionStatus.SUCCEEDED,
                correlation_id=context.correlation_id,
                trace_id=context.trace_id,
            )

    provider: ModelProvider = Provider()
    assert asyncio.iscoroutinefunction(provider.invoke_model)
    assert ModelRole.ROUTING == "routing"
