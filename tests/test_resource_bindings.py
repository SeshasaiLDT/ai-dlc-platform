"""Resource Binding Registry lifecycle and cross-initiative security contracts."""

from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dlc.adapters.authorization import InMemoryMembershipRepository
from ai_dlc.adapters.resource_bindings import (
    InMemoryBindingEventSink,
    InMemoryResourceBindingRepository,
)
from ai_dlc.application.authorization import (
    RoleGrant,
    RolePolicy,
    ScopeRestriction,
    resolve_authorization_context,
)
from ai_dlc.application.resource_bindings import (
    ArtifactStoreBinding,
    BindingConflictError,
    BindingExistsError,
    BindingIsolationError,
    BindingNotFoundError,
    BindingResolutionDeniedError,
    BindingStatus,
    GitRepositoryBinding,
    JiraBinding,
    KnowledgeBinding,
    KnowledgeIsolation,
    LogicalResourceRef,
    MetadataFilter,
    ResourceAccess,
    ResourceBinding,
    ResourceBindingKey,
    ResourceBindingRegistry,
    ResourceType,
    ServiceNowBinding,
    TrustedResolutionContext,
)
from ai_dlc.domain.identity import InitiativeMembership, Principal, Role, ToolPermission
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile
from ai_dlc.domain.initiative.enums import GitProvider

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"
NOW = datetime(2026, 10, 7, tzinfo=UTC)
PRINCIPAL = Principal("person-1", "test")
PERMISSIONS = frozenset(ToolPermission)


def profile(name: str) -> InitiativeProfile:
    original = load_initiative_profile(EXAMPLES / name)
    document = original.model_dump(mode="json")
    document["artifacts"] = {"stores": [{"id": "deliverables"}]}
    return InitiativeProfile.model_validate(document)


TRAVEL = profile("travel-platform.yaml")
FIELD = profile("field-operations.yaml")


def context(
    selected: InitiativeProfile,
    *,
    environment: str = "dev",
    restriction: ScopeRestriction | None = None,
    permissions: frozenset[ToolPermission] = PERMISSIONS,
) -> TrustedResolutionContext:
    member = InitiativeMembership(PRINCIPAL.subject_id, selected.initiative.id, (Role.ANALYST,))
    authorization = resolve_authorization_context(
        PRINCIPAL,
        selected.initiative.id,
        selected,
        InMemoryMembershipRepository((member,)),
        RolePolicy((RoleGrant(Role.ANALYST, tool_permissions=permissions),)),
        scope_restriction=restriction,
    )
    return TrustedResolutionContext(environment, selected, authorization, "trace-1")


def registry() -> tuple[ResourceBindingRegistry, InMemoryBindingEventSink]:
    events = InMemoryBindingEventSink()
    return (
        ResourceBindingRegistry(
            InMemoryResourceBindingRepository(),
            events,
            environments=frozenset({"dev", "demo"}),
            clock=lambda: NOW,
        ),
        events,
    )


def key(
    selected: InitiativeProfile,
    kind: ResourceType,
    logical_id: str,
    environment: str = "dev",
) -> ResourceBindingKey:
    return ResourceBindingKey(environment, selected.initiative.id, kind, logical_id)


def ref(kind: ResourceType, logical_id: str, target: str | None = None) -> LogicalResourceRef:
    return LogicalResourceRef(kind, logical_id, target)


def test_registry_lifecycle_revision_and_audit_contains_only_logical_metadata() -> None:
    service, events = registry()
    binding_key = key(TRAVEL, ResourceType.JIRA, "jira")
    first = service.register(
        binding_key, JiraBinding("corporate-jira", "primary-site"), correlation_id="c1"
    )
    assert service.get(binding_key) == first
    assert service.list(environment="dev", initiative_id=TRAVEL.initiative.id) == (first,)
    assert service.list(environment="demo", initiative_id=TRAVEL.initiative.id) == ()
    with pytest.raises(BindingExistsError):
        service.register(binding_key, first.details, correlation_id="c2")
    with pytest.raises(BindingNotFoundError):
        service.get(key(TRAVEL, ResourceType.JIRA, "jira", "demo"))
    second = service.update(
        binding_key,
        JiraBinding("secondary-jira", "second-site"),
        expected_revision=1,
        correlation_id="c3",
    )
    assert second.revision == 2 and second.details.connection_alias == "secondary-jira"
    with pytest.raises(BindingConflictError):
        service.update(binding_key, first.details, expected_revision=1, correlation_id="c4")
    disabled = service.disable(binding_key, expected_revision=2, correlation_id="c5")
    assert disabled.status is BindingStatus.DISABLED and disabled.revision == 3
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.JIRA, "jira", "TRAVEL"),
            context=context(TRAVEL),
            access=ResourceAccess.READ,
        )
    assert [event.event_type.value for event in events.events] == ["created", "updated", "disabled"]
    assert all(not hasattr(event, "details") for event in events.events)
    assert all(event.key == binding_key for event in events.events)


def test_jira_connection_is_independent_of_project_scope_and_access() -> None:
    service, _ = registry()
    service.register(
        key(TRAVEL, ResourceType.JIRA, "jira"),
        JiraBinding("jira-dev", "site-one"),
        correlation_id="c",
    )
    resolved = service.resolve(
        ref(ResourceType.JIRA, "jira", "TRAVEL"),
        context=context(TRAVEL),
        access=ResourceAccess.READ,
    )
    assert resolved.binding.details.site_alias == "site-one"
    assert "projects" not in asdict(resolved.binding.details)
    narrowed = context(TRAVEL, restriction=ScopeRestriction(jira_projects=frozenset({"JOURNEY"})))
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.JIRA, "jira", "TRAVEL"), context=narrowed, access=ResourceAccess.READ
        )
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.JIRA, "jira", "FOREIGN"),
            context=context(TRAVEL),
            access=ResourceAccess.READ,
        )
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.JIRA, "jira", "TRAVEL"),
            context=context(TRAVEL, permissions=frozenset({ToolPermission.JIRA_READ})),
            access=ResourceAccess.WRITE,
        )


def test_wrong_initiative_environment_and_logical_id_fail_closed() -> None:
    service, _ = registry()
    service.register(
        key(TRAVEL, ResourceType.KNOWLEDGE, "architecture"),
        KnowledgeBinding("vector-a", "chunks", KnowledgeIsolation.DEDICATED_INSTANCE),
        correlation_id="c",
    )
    logical = ref(ResourceType.KNOWLEDGE, "architecture")
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(logical, context=context(FIELD), access=ResourceAccess.READ)
    with pytest.raises(BindingNotFoundError):
        service.resolve(
            logical, context=context(TRAVEL, environment="demo"), access=ResourceAccess.READ
        )
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.KNOWLEDGE, "unknown"),
            context=context(TRAVEL),
            access=ResourceAccess.READ,
        )
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            logical, context=context(TRAVEL, environment="other"), access=ResourceAccess.READ
        )
    with pytest.raises(ValueError):
        TrustedResolutionContext("dev", FIELD, context(TRAVEL).authorization, "c")


def test_dedicated_and_shared_separate_knowledge_locations() -> None:
    service, _ = registry()
    dedicated = KnowledgeBinding("vector-a", "chunks", KnowledgeIsolation.DEDICATED_INSTANCE)
    service.register(
        key(TRAVEL, ResourceType.KNOWLEDGE, "architecture"), dedicated, correlation_id="c"
    )
    with pytest.raises(BindingIsolationError):
        service.register(
            key(FIELD, ResourceType.KNOWLEDGE, "runbooks"),
            KnowledgeBinding("vector-a", "other_chunks", KnowledgeIsolation.SEPARATE_LOCATION),
            correlation_id="c",
        )
    assert (
        service.resolve(
            ref(ResourceType.KNOWLEDGE, "architecture"),
            context=context(TRAVEL),
            access=ResourceAccess.READ,
        ).binding.details
        == dedicated
    )

    shared, _ = registry()
    first = KnowledgeBinding(
        "shared-vector", "chunks", KnowledgeIsolation.SEPARATE_LOCATION, "travel"
    )
    second = KnowledgeBinding(
        "shared-vector", "chunks", KnowledgeIsolation.SEPARATE_LOCATION, "field"
    )
    shared.register(key(TRAVEL, ResourceType.KNOWLEDGE, "architecture"), first, correlation_id="c")
    shared.register(key(FIELD, ResourceType.KNOWLEDGE, "runbooks"), second, correlation_id="c")
    assert (
        shared.resolve(
            ref(ResourceType.KNOWLEDGE, "runbooks"),
            context=context(FIELD),
            access=ResourceAccess.READ,
        ).binding.details.namespace
        == "field"
    )
    with pytest.raises(BindingIsolationError):
        shared.register(
            key(FIELD, ResourceType.KNOWLEDGE, "incident-history"),
            KnowledgeBinding(
                "shared-vector", "chunks", KnowledgeIsolation.SEPARATE_LOCATION, "travel"
            ),
            correlation_id="c",
        )


def test_shared_knowledge_filter_is_mandatory_immutable_and_initiative_bound() -> None:
    service, _ = registry()
    shared_key = key(TRAVEL, ResourceType.KNOWLEDGE, "architecture")
    with pytest.raises(BindingIsolationError):
        service.register(
            shared_key,
            KnowledgeBinding(
                "shared-vector", "chunks", KnowledgeIsolation.FILTERED_SHARED_LOCATION
            ),
            correlation_id="c",
        )
    with pytest.raises(BindingIsolationError):
        service.register(
            shared_key,
            KnowledgeBinding(
                "shared-vector",
                "chunks",
                KnowledgeIsolation.FILTERED_SHARED_LOCATION,
                mandatory_filters=(MetadataFilter("initiative_id", FIELD.initiative.id),),
            ),
            correlation_id="c",
        )
    first = KnowledgeBinding(
        "shared-vector",
        "chunks",
        KnowledgeIsolation.FILTERED_SHARED_LOCATION,
        mandatory_filters=(MetadataFilter("initiative_id", TRAVEL.initiative.id),),
    )
    second = KnowledgeBinding(
        "shared-vector",
        "chunks",
        KnowledgeIsolation.FILTERED_SHARED_LOCATION,
        mandatory_filters=(MetadataFilter("initiative_id", FIELD.initiative.id),),
    )
    service.register(shared_key, first, correlation_id="c")
    service.register(key(FIELD, ResourceType.KNOWLEDGE, "runbooks"), second, correlation_id="c")
    resolved = service.resolve(
        ref(ResourceType.KNOWLEDGE, "architecture"),
        context=context(TRAVEL),
        access=ResourceAccess.READ,
    )
    assert resolved.binding.details.mandatory_filters == (
        MetadataFilter("initiative_id", TRAVEL.initiative.id),
    )
    with pytest.raises(FrozenInstanceError):
        resolved.binding.details.mandatory_filters = ()
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.KNOWLEDGE, "architecture"),
            context=context(FIELD),
            access=ResourceAccess.READ,
        )
    with pytest.raises(BindingIsolationError):
        service.update(
            shared_key,
            KnowledgeBinding("shared-vector", "chunks", KnowledgeIsolation.SEPARATE_LOCATION),
            expected_revision=1,
            correlation_id="c",
        )


def test_servicenow_git_and_artifact_bindings_stay_within_profile_scope() -> None:
    service, _ = registry()
    service.register(
        key(FIELD, ResourceType.SERVICENOW, "servicenow"),
        ServiceNowBinding("snow-dev", "instance-one"),
        correlation_id="c",
    )
    assert (
        service.resolve(
            ref(ResourceType.SERVICENOW, "servicenow", "incident"),
            context=context(FIELD),
            access=ResourceAccess.READ,
        ).binding.details.connection_alias
        == "snow-dev"
    )
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.SERVICENOW, "servicenow", "other"),
            context=context(FIELD),
            access=ResourceAccess.READ,
        )
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.SERVICENOW, "servicenow", "incident"),
            context=context(
                FIELD, restriction=ScopeRestriction(servicenow_scopes=frozenset({"change"}))
            ),
            access=ResourceAccess.READ,
        )
    service.register(
        key(TRAVEL, ResourceType.GIT_REPOSITORY, "travel-api"),
        GitRepositoryBinding(GitProvider.GITHUB, "git-dev", "approved-org", "api"),
        correlation_id="c",
    )
    assert (
        service.resolve(
            ref(ResourceType.GIT_REPOSITORY, "travel-api"),
            context=context(TRAVEL),
            access=ResourceAccess.READ,
        ).binding.details.provider
        is GitProvider.GITHUB
    )
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.GIT_REPOSITORY, "travel-api"),
            context=context(FIELD),
            access=ResourceAccess.READ,
        )
    service.register(
        key(TRAVEL, ResourceType.ARTIFACT_STORE, "deliverables"),
        ArtifactStoreBinding("artifact-bucket", "travel/outputs"),
        correlation_id="c",
    )
    assert (
        service.resolve(
            ref(ResourceType.ARTIFACT_STORE, "deliverables"),
            context=context(TRAVEL),
            access=ResourceAccess.WRITE,
        ).binding.details.prefix
        == "travel/outputs"
    )
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.ARTIFACT_STORE, "deliverables"),
            context=context(TRAVEL, restriction=ScopeRestriction(artifact_store_ids=frozenset())),
            access=ResourceAccess.READ,
        )
    with pytest.raises(BindingIsolationError):
        service.register(
            key(FIELD, ResourceType.ARTIFACT_STORE, "deliverables"),
            ArtifactStoreBinding("artifact-bucket", "travel"),
            correlation_id="c",
        )


def test_binding_models_reject_physical_agent_fields_secrets_and_wrong_types() -> None:
    with pytest.raises(TypeError):
        LogicalResourceRef(ResourceType.KNOWLEDGE, "architecture", table="chunks")
    with pytest.raises(TypeError):
        LogicalResourceRef(
            ResourceType.JIRA, "jira", target_id="TRAVEL", site_url="https://example.test"
        )
    with pytest.raises(ValueError):
        JiraBinding("https://example.test", "site")
    with pytest.raises(ValueError):
        JiraBinding("jira-dev", "arn:aws:secretsmanager:us-east-1:123:secret:token")
    with pytest.raises(ValueError):
        ArtifactStoreBinding("bucket", "../escape")
    with pytest.raises(ValueError):
        ResourceBindingKey("dev", "travel-platform", ResourceType.JIRA, "TRAVEL")
    with pytest.raises(ValueError):
        ResourceBinding(
            key(TRAVEL, ResourceType.JIRA, "jira"),
            ArtifactStoreBinding("bucket", "safe/path"),
            BindingStatus.ACTIVE,
            1,
            NOW,
            NOW,
        )
    with pytest.raises(ValidationError):
        InitiativeProfile.model_validate(
            {**TRAVEL.model_dump(), "artifacts": {"stores": [{"id": "x", "bucket": "raw"}]}}
        )
    public_fields = {field.name for field in fields(LogicalResourceRef)}
    assert public_fields == {"resource_type", "logical_resource_id", "target_id"}
    assert not public_fields & {
        "table",
        "namespace",
        "bucket",
        "endpoint",
        "secret_arn",
        "connection_alias",
        "site_url",
    }
    assert not {"password", "token", "endpoint", "secret_arn"} & {
        field.name for field in fields(JiraBinding)
    }
    service, _ = registry()
    safe = service.register(
        key(TRAVEL, ResourceType.JIRA, "jira"),
        JiraBinding("jira-dev", "site-one"),
        correlation_id="c",
    )
    serialized = str(asdict(safe))
    assert "password" not in serialized and "token" not in serialized
    assert "https://" not in serialized and "arn:" not in serialized


def test_profile_change_or_scope_narrowing_never_expands_resolution() -> None:
    service, _ = registry()
    service.register(
        key(TRAVEL, ResourceType.KNOWLEDGE, "architecture"),
        KnowledgeBinding("vector-a", "chunks", KnowledgeIsolation.DEDICATED_INSTANCE),
        correlation_id="c",
    )
    narrowed = context(TRAVEL, restriction=ScopeRestriction(knowledge_source_ids=frozenset()))
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.KNOWLEDGE, "architecture"),
            context=narrowed,
            access=ResourceAccess.READ,
        )
    document = TRAVEL.model_dump(mode="json")
    document["knowledge"]["sources"] = [
        source for source in document["knowledge"]["sources"] if source["id"] != "architecture"
    ]
    revised = InitiativeProfile.model_validate(document)
    stale = replace(context(TRAVEL), profile=revised)
    with pytest.raises(BindingResolutionDeniedError):
        service.resolve(
            ref(ResourceType.KNOWLEDGE, "architecture"), context=stale, access=ResourceAccess.READ
        )
