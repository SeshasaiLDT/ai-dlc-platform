"""Stable application API for registered initiative configuration."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from ai_dlc.domain.initiative import InitiativeProfile

from .errors import InitiativeIdentityMismatchError, InitiativeNotFoundError
from .models import InitiativeStatus, RegisteredInitiative, RegistryEventType, RegistryMutationEvent
from .ports import InitiativeRepository, RegistryEventSink


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InitiativeRegistry:
    """Consumer-facing boundary; storage and event delivery are injected ports."""

    def __init__(
        self,
        repository: InitiativeRepository,
        event_sink: RegistryEventSink,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._event_sink = event_sink
        self._clock = clock

    def create(self, profile: InitiativeProfile) -> RegisteredInitiative:
        now = self._now()
        registered = RegisteredInitiative(profile, InitiativeStatus.ACTIVE, now, now)
        self._repository.insert(registered)
        self._emit(RegistryEventType.INITIATIVE_CREATED, registered, before=None)
        return registered

    def get(self, initiative_id: str) -> RegisteredInitiative:
        registered = self._repository.find_by_id(initiative_id)
        if registered is None:
            raise InitiativeNotFoundError(initiative_id)
        return registered

    def list(self, *, status: InitiativeStatus | None = None) -> tuple[RegisteredInitiative, ...]:
        """List all registrations by stable ID, optionally filtered by lifecycle state."""
        records = self._repository.list_all()
        if status is not None:
            records = tuple(record for record in records if record.status == status)
        return tuple(sorted(records, key=lambda record: record.id))

    def update(self, initiative_id: str, profile: InitiativeProfile) -> RegisteredInitiative:
        if profile.initiative.id != initiative_id:
            raise InitiativeIdentityMismatchError(initiative_id, profile.initiative.id)
        current = self.get(initiative_id)
        updated = replace(current, profile=profile, updated_at=self._now())
        self._repository.replace(updated)
        self._emit(RegistryEventType.INITIATIVE_UPDATED, updated, before=current.status)
        return updated

    def disable(self, initiative_id: str) -> RegisteredInitiative:
        """Disable once; subsequent calls return the same record without another event."""
        current = self.get(initiative_id)
        if current.status == InitiativeStatus.DISABLED:
            return current
        disabled = replace(current, status=InitiativeStatus.DISABLED, updated_at=self._now())
        self._repository.replace(disabled)
        self._emit(RegistryEventType.INITIATIVE_DISABLED, disabled, before=current.status)
        return disabled

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Registry clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _emit(
        self,
        event_type: RegistryEventType,
        registered: RegisteredInitiative,
        *,
        before: InitiativeStatus | None,
    ) -> None:
        event = RegistryMutationEvent(
            event_type=event_type,
            initiative_id=registered.id,
            occurred_at=registered.updated_at,
            metadata={
                "schema_version": registered.profile.schema_version,
                "status_before": before.value if before is not None else "unregistered",
                "status_after": registered.status.value,
            },
        )
        self._event_sink.publish(event)
