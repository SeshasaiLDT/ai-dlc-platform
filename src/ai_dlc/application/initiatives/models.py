"""Registry-owned lifecycle records and mutation events."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
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


@dataclass(frozen=True, slots=True)
class RegisteredInitiative:
    """An immutable profile plus registration metadata owned by the Registry."""

    profile: InitiativeProfile
    status: InitiativeStatus
    created_at: datetime
    updated_at: datetime

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
