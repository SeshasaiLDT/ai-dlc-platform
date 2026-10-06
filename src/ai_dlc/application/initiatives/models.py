"""Registry-owned lifecycle records and mutation events."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from ai_dlc.domain.initiative import InitiativeProfile


class InitiativeStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class RegistryEventType(StrEnum):
    INITIATIVE_CREATED = "initiative_created"
    INITIATIVE_UPDATED = "initiative_updated"
    INITIATIVE_DISABLED = "initiative_disabled"
    INITIATIVE_ROLLED_BACK = "initiative_rolled_back"


class ConfigurationRevisionReason(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class ConfigurationRevision:
    """One immutable configuration snapshot, numbered within its initiative."""

    initiative_id: str
    revision: int
    profile: InitiativeProfile
    created_at: datetime
    reason: ConfigurationRevisionReason
    source_revision: int | None = None

    def __post_init__(self) -> None:
        if self.initiative_id != self.profile.initiative.id:
            raise ValueError("revision initiative ID must match its profile")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != timedelta(0):
            raise ValueError("revision timestamp must be timezone-aware UTC")
        if not isinstance(self.reason, ConfigurationRevisionReason):
            raise ValueError("revision reason must be a ConfigurationRevisionReason")
        if self.reason == ConfigurationRevisionReason.CREATED and self.revision != 1:
            raise ValueError("created revision must be revision 1")
        if self.reason != ConfigurationRevisionReason.CREATED and self.revision == 1:
            raise ValueError("revision 1 must have created reason")
        if self.reason == ConfigurationRevisionReason.ROLLBACK:
            if (
                type(self.source_revision) is not int
                or self.source_revision < 1
                or self.source_revision >= self.revision
            ):
                raise ValueError("rollback source must be an earlier positive revision")
        elif self.source_revision is not None:
            raise ValueError("source revision is only recorded for rollback")


@dataclass(frozen=True, slots=True)
class RegisteredInitiative:
    """An immutable profile plus registration metadata owned by the Registry."""

    profile: InitiativeProfile
    status: InitiativeStatus
    created_at: datetime
    updated_at: datetime
    current_revision: int = 1

    @property
    def id(self) -> str:
        return self.profile.initiative.id


@dataclass(frozen=True, slots=True)
class RegistryMutationEvent:
    """Small application event for a future durable audit implementation."""

    event_type: RegistryEventType
    initiative_id: str
    occurred_at: datetime
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
