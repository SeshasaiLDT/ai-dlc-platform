"""Typed, secret-free resource bindings and logical resolution requests."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from re import fullmatch

from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.domain.initiative import InitiativeProfile
from ai_dlc.domain.initiative.enums import GitProvider


def _id(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) is None
        or len(value) > 80
    ):
        raise ValueError(f"invalid {field}")


def _alias(value: str, field: str) -> None:
    # A managed connection/configuration name, never a URL, ARN, or secret value.
    if (
        not isinstance(value, str)
        or fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value) is None
        or len(value) > 80
    ):
        raise ValueError(f"invalid {field} alias")


def _sql_name(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None
        or len(value) > 63
    ):
        raise ValueError(f"invalid {field}")


class ResourceType(StrEnum):
    JIRA = "jira"
    SERVICENOW = "servicenow"
    GIT_REPOSITORY = "git_repository"
    KNOWLEDGE = "knowledge"
    ARTIFACT_STORE = "artifact_store"


class BindingStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ResourceAccess(StrEnum):
    READ = "read"
    WRITE = "write"


class KnowledgeIsolation(StrEnum):
    DEDICATED_INSTANCE = "dedicated_instance"
    SEPARATE_LOCATION = "separate_location"
    FILTERED_SHARED_LOCATION = "filtered_shared_location"


def _logical_resource_id(resource_type: ResourceType, value: str) -> None:
    if resource_type is ResourceType.JIRA:
        if value != "jira":
            raise ValueError("Jira connection binding ID must be 'jira'")
    elif resource_type is ResourceType.SERVICENOW:
        if value != "servicenow":
            raise ValueError("ServiceNow connection binding ID must be 'servicenow'")
    else:
        _id(value, "logical_resource_id")


@dataclass(frozen=True, slots=True)
class ResourceBindingKey:
    environment: str
    initiative_id: str
    resource_type: ResourceType
    logical_resource_id: str

    def __post_init__(self) -> None:
        _id(self.environment, "environment")
        _id(self.initiative_id, "initiative_id")
        if not isinstance(self.resource_type, ResourceType):
            raise ValueError("unsupported resource type")
        _logical_resource_id(self.resource_type, self.logical_resource_id)


@dataclass(frozen=True, slots=True)
class JiraBinding:
    connection_alias: str
    site_alias: str

    def __post_init__(self) -> None:
        _alias(self.connection_alias, "connection")
        _alias(self.site_alias, "site")


@dataclass(frozen=True, slots=True)
class ServiceNowBinding:
    connection_alias: str
    instance_alias: str

    def __post_init__(self) -> None:
        _alias(self.connection_alias, "connection")
        _alias(self.instance_alias, "instance")


@dataclass(frozen=True, slots=True)
class GitRepositoryBinding:
    provider: GitProvider
    connection_alias: str
    owner: str
    repository: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider, GitProvider):
            raise ValueError("unsupported Git provider")
        _alias(self.connection_alias, "connection")
        for name in ("owner", "repository"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None
                or len(value) > 100
            ):
                raise ValueError(f"invalid Git {name}")


@dataclass(frozen=True, slots=True)
class MetadataFilter:
    field: str
    value: str

    def __post_init__(self) -> None:
        _sql_name(self.field, "metadata filter field")
        if not isinstance(self.value, str) or not self.value or len(self.value) > 200:
            raise ValueError("metadata filter value must be nonblank and bounded")


@dataclass(frozen=True, slots=True)
class KnowledgeBinding:
    connection_alias: str
    table: str
    isolation: KnowledgeIsolation
    namespace: str | None = None
    mandatory_filters: tuple[MetadataFilter, ...] = ()

    def __post_init__(self) -> None:
        _alias(self.connection_alias, "connection")
        _sql_name(self.table, "table")
        if self.namespace is not None:
            _sql_name(self.namespace, "namespace")
        if not isinstance(self.isolation, KnowledgeIsolation):
            raise ValueError("unsupported knowledge isolation")
        filters = tuple(self.mandatory_filters)
        if any(not isinstance(item, MetadataFilter) for item in filters):
            raise ValueError("invalid mandatory metadata filter")
        if len({item.field for item in filters}) != len(filters):
            raise ValueError("duplicate metadata filter field")
        object.__setattr__(self, "mandatory_filters", filters)


@dataclass(frozen=True, slots=True)
class ArtifactStoreBinding:
    bucket_alias: str
    prefix: str

    def __post_init__(self) -> None:
        _alias(self.bucket_alias, "bucket")
        if (
            not isinstance(self.prefix, str)
            or not self.prefix
            or self.prefix.startswith("/")
            or "\\" in self.prefix
            or any(part in ("", ".", "..") for part in self.prefix.split("/"))
        ):
            raise ValueError("artifact prefix must be a contained relative path")


BindingDetails = (
    JiraBinding | ServiceNowBinding | GitRepositoryBinding | KnowledgeBinding | ArtifactStoreBinding
)

_DETAIL_TYPES: dict[ResourceType, type] = {
    ResourceType.JIRA: JiraBinding,
    ResourceType.SERVICENOW: ServiceNowBinding,
    ResourceType.GIT_REPOSITORY: GitRepositoryBinding,
    ResourceType.KNOWLEDGE: KnowledgeBinding,
    ResourceType.ARTIFACT_STORE: ArtifactStoreBinding,
}


@dataclass(frozen=True, slots=True)
class ResourceBinding:
    key: ResourceBindingKey
    details: BindingDetails
    status: BindingStatus
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.key, ResourceBindingKey) or not isinstance(
            self.details, _DETAIL_TYPES[self.key.resource_type]
        ):
            raise ValueError("binding type does not match key")
        if not isinstance(self.status, BindingStatus):
            raise ValueError("invalid binding status")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("binding revision must be positive")
        for timestamp in (self.created_at, self.updated_at):
            if not isinstance(timestamp, datetime) or timestamp.utcoffset() != timedelta(0):
                raise ValueError("binding timestamps must be UTC")
        if self.updated_at < self.created_at:
            raise ValueError("binding update precedes creation")


@dataclass(frozen=True, slots=True)
class LogicalResourceRef:
    """The only resource-routing fields accepted from an agent-facing request."""

    resource_type: ResourceType
    logical_resource_id: str
    target_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resource_type, ResourceType):
            raise ValueError("unsupported resource type")
        if self.resource_type is ResourceType.JIRA:
            if (
                not isinstance(self.target_id, str)
                or fullmatch(r"[A-Z][A-Z0-9_]*", self.target_id) is None
                or len(self.target_id) > 20
            ):
                raise ValueError("Jira project target required")
        elif self.resource_type is ResourceType.SERVICENOW:
            if (
                not isinstance(self.target_id, str)
                or fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", self.target_id) is None
                or len(self.target_id) > 80
            ):
                raise ValueError("ServiceNow scope target required")
        elif self.target_id is not None:
            raise ValueError("target_id is only used for Jira and ServiceNow")
        _logical_resource_id(self.resource_type, self.logical_resource_id)


@dataclass(frozen=True, slots=True)
class TrustedResolutionContext:
    """Created by the platform after authentication and authorization."""

    environment: str
    profile: InitiativeProfile
    authorization: ResolvedAuthorizationContext
    correlation_id: str

    def __post_init__(self) -> None:
        _id(self.environment, "environment")
        if not isinstance(self.profile, InitiativeProfile) or not isinstance(
            self.authorization, ResolvedAuthorizationContext
        ):
            raise TypeError("trusted resolution requires Profile and authorization context")
        if self.profile.initiative.id != self.authorization.initiative_id:
            raise ValueError("Profile and authorization initiative mismatch")
        if not isinstance(self.correlation_id, str) or not self.correlation_id.strip():
            raise ValueError("correlation_id must be nonblank")


@dataclass(frozen=True, slots=True)
class ResolvedResource:
    """Trusted adapter-only context; never serialize this object into an agent result."""

    binding: ResourceBinding
    correlation_id: str


class BindingEventType(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class BindingMutationEvent:
    event_type: BindingEventType
    key: ResourceBindingKey
    revision: int
    occurred_at: datetime
    correlation_id: str
