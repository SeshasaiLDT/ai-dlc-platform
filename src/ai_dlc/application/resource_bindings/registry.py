"""Trusted logical-to-physical resolution after initiative authorization."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from ai_dlc.domain.identity import ToolPermission

from .errors import (
    BindingAuditError,
    BindingNotFoundError,
    BindingResolutionDeniedError,
)
from .models import (
    BindingDetails,
    BindingEventType,
    BindingMutationEvent,
    BindingStatus,
    GitRepositoryBinding,
    KnowledgeBinding,
    KnowledgeIsolation,
    LogicalResourceRef,
    ResolvedResource,
    ResourceAccess,
    ResourceBinding,
    ResourceBindingKey,
    ResourceType,
    TrustedResolutionContext,
)
from .ports import BindingEventSink, ResourceBindingRepository


def _now() -> datetime:
    return datetime.now(UTC)


_PERMISSIONS = {
    (ResourceType.JIRA, ResourceAccess.READ): ToolPermission.JIRA_READ,
    (ResourceType.JIRA, ResourceAccess.WRITE): ToolPermission.JIRA_WRITE,
    (ResourceType.SERVICENOW, ResourceAccess.READ): ToolPermission.SERVICENOW_READ,
    (ResourceType.SERVICENOW, ResourceAccess.WRITE): ToolPermission.SERVICENOW_WRITE,
    (ResourceType.GIT_REPOSITORY, ResourceAccess.READ): ToolPermission.GIT_READ,
    (ResourceType.GIT_REPOSITORY, ResourceAccess.WRITE): ToolPermission.GIT_WRITE,
    (ResourceType.KNOWLEDGE, ResourceAccess.READ): ToolPermission.KNOWLEDGE_READ,
    (ResourceType.ARTIFACT_STORE, ResourceAccess.READ): ToolPermission.ARTIFACT_READ,
    (ResourceType.ARTIFACT_STORE, ResourceAccess.WRITE): ToolPermission.ARTIFACT_WRITE,
}


class ResourceBindingRegistry:
    """Control-plane registry. Do not expose get/list/resolve directly to agents."""

    def __init__(
        self,
        repository: ResourceBindingRepository,
        event_sink: BindingEventSink,
        *,
        environments: frozenset[str],
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if not environments or any(
            not isinstance(value, str) or not value for value in environments
        ):
            raise ValueError("approved environments required")
        for value in environments:
            ResourceBindingKey(value, "validation", ResourceType.JIRA, "jira")
        self._repository = repository
        self._events = event_sink
        self._environments = frozenset(environments)
        self._clock = clock

    def register(
        self, key: ResourceBindingKey, details: BindingDetails, *, correlation_id: str
    ) -> ResourceBinding:
        self._check_environment(key.environment)
        self._check_correlation_id(correlation_id)
        now = self._now()
        binding = ResourceBinding(key, details, BindingStatus.ACTIVE, 1, now, now)
        self._repository.insert(binding)
        self._emit(BindingEventType.CREATED, binding, correlation_id)
        return binding

    def get(self, key: ResourceBindingKey) -> ResourceBinding:
        self._check_environment(key.environment)
        binding = self._repository.find(key)
        if binding is None or binding.key != key:
            raise BindingNotFoundError("resource binding not found")
        return binding

    def list(self, *, environment: str, initiative_id: str) -> tuple[ResourceBinding, ...]:
        self._check_environment(environment)
        return tuple(
            sorted(
                (
                    item
                    for item in self._repository.list_all()
                    if item.key.environment == environment
                    and item.key.initiative_id == initiative_id
                ),
                key=lambda item: (item.key.resource_type.value, item.key.logical_resource_id),
            )
        )

    def update(
        self,
        key: ResourceBindingKey,
        details: BindingDetails,
        *,
        expected_revision: int,
        correlation_id: str,
    ) -> ResourceBinding:
        current = self.get(key)
        self._check_correlation_id(correlation_id)
        updated = replace(
            current,
            details=details,
            status=BindingStatus.ACTIVE,
            revision=current.revision + 1,
            updated_at=self._now(),
        )
        self._repository.replace(updated, expected_revision=expected_revision)
        self._emit(BindingEventType.UPDATED, updated, correlation_id)
        return updated

    def disable(
        self, key: ResourceBindingKey, *, expected_revision: int, correlation_id: str
    ) -> ResourceBinding:
        current = self.get(key)
        self._check_correlation_id(correlation_id)
        if current.status is BindingStatus.DISABLED:
            return current
        disabled = replace(
            current,
            status=BindingStatus.DISABLED,
            revision=current.revision + 1,
            updated_at=self._now(),
        )
        self._repository.replace(disabled, expected_revision=expected_revision)
        self._emit(BindingEventType.DISABLED, disabled, correlation_id)
        return disabled

    def resolve(
        self,
        reference: LogicalResourceRef,
        *,
        context: TrustedResolutionContext,
        access: ResourceAccess,
    ) -> ResolvedResource:
        """Only trusted callers inject context after operation policy/approval checks."""
        if (
            not isinstance(reference, LogicalResourceRef)
            or not isinstance(context, TrustedResolutionContext)
            or not isinstance(access, ResourceAccess)
        ):
            raise TypeError("invalid resolution request")
        self._check_environment(context.environment)
        permission = _PERMISSIONS.get((reference.resource_type, access))
        if permission is None or not context.authorization.can_use_tool(permission):
            raise BindingResolutionDeniedError("resource permission denied")
        if not self._in_scope(reference, context):
            raise BindingResolutionDeniedError("logical resource outside authorized initiative")
        key = ResourceBindingKey(
            context.environment,
            context.authorization.initiative_id,
            reference.resource_type,
            reference.logical_resource_id,
        )
        binding = self.get(key)
        if binding.status is not BindingStatus.ACTIVE:
            raise BindingResolutionDeniedError("resource binding disabled")
        if isinstance(binding.details, KnowledgeBinding):
            self._check_knowledge_filter(binding)
        if isinstance(binding.details, GitRepositoryBinding):
            if binding.details.provider != context.profile.integrations.git.provider:
                raise BindingResolutionDeniedError("Git provider differs from Initiative Profile")
        return ResolvedResource(binding, context.correlation_id)

    @staticmethod
    def _in_scope(reference: LogicalResourceRef, context: TrustedResolutionContext) -> bool:
        profile = context.profile
        scopes = context.authorization.allowed_scopes
        kind = reference.resource_type
        if kind is ResourceType.JIRA:
            return (
                profile.integrations.jira.enabled
                and reference.target_id in profile.integrations.jira.projects
                and reference.target_id in scopes.jira_projects
            )
        if kind is ResourceType.SERVICENOW:
            return (
                profile.integrations.servicenow.enabled
                and reference.target_id in profile.integrations.servicenow.scopes
                and reference.target_id in scopes.servicenow_scopes
            )
        if kind is ResourceType.GIT_REPOSITORY:
            return (
                profile.integrations.git.enabled
                and reference.logical_resource_id
                in {item.id for item in profile.integrations.git.repositories}
                and reference.logical_resource_id in scopes.repository_ids
            )
        if kind is ResourceType.KNOWLEDGE:
            return (
                reference.logical_resource_id
                in {item.id for item in profile.knowledge.sources if item.enabled}
                and reference.logical_resource_id in scopes.knowledge_source_ids
            )
        return (
            reference.logical_resource_id in {item.id for item in profile.artifacts.stores}
            and reference.logical_resource_id in scopes.artifact_store_ids
        )

    @staticmethod
    def _check_knowledge_filter(binding: ResourceBinding) -> None:
        details = binding.details
        if (
            isinstance(details, KnowledgeBinding)
            and details.isolation is KnowledgeIsolation.FILTERED_SHARED_LOCATION
            and not any(
                item.field == "initiative_id" and item.value == binding.key.initiative_id
                for item in details.mandatory_filters
            )
        ):
            raise BindingResolutionDeniedError("mandatory initiative filter missing")

    def _check_environment(self, environment: str) -> None:
        if environment not in self._environments:
            raise BindingResolutionDeniedError("environment not approved")

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("binding clock must be timezone-aware")
        return value.astimezone(UTC)

    def _emit(
        self, event_type: BindingEventType, binding: ResourceBinding, correlation_id: str
    ) -> None:
        event = BindingMutationEvent(
            event_type, binding.key, binding.revision, binding.updated_at, correlation_id
        )
        try:
            self._events.publish(event)
        except Exception:
            raise BindingAuditError("binding event publication failed") from None

    @staticmethod
    def _check_correlation_id(correlation_id: str) -> None:
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError("correlation_id must be nonblank")
