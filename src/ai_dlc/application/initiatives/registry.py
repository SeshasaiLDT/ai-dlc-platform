"""Stable application API for registered initiative configuration."""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from ai_dlc.domain.initiative import InitiativeProfile

from .errors import (
    InitiativeIdentityMismatchError,
    InitiativeNotFoundError,
    InitiativeRevisionNotFoundError,
)
from .models import (
    ConfigurationRevision,
    ConfigurationRevisionReason,
    InitiativeStatus,
    RegisteredInitiative,
    RegistryEventType,
    RegistryMutationEvent,
)
from .ports import InitiativeRevisionRepository, RegistryEventSink


def _utc_now() -> datetime:
    return datetime.now(UTC)


class InitiativeRegistry:
    """Consumer-facing boundary; storage and event delivery are injected ports."""

    def __init__(
        self,
        repository: InitiativeRevisionRepository,
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
        revision = ConfigurationRevision(
            profile.initiative.id, 1, profile, now, ConfigurationRevisionReason.CREATED
        )
        self._repository.insert_with_revision(registered, revision)
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
        now = self._now()
        new_number = current.current_revision + 1
        revision = ConfigurationRevision(
            initiative_id, new_number, profile, now, ConfigurationRevisionReason.UPDATED
        )
        updated = replace(current, profile=profile, current_revision=new_number, updated_at=now)
        self._repository.append_revision(updated, revision, expected_current=current)
        self._emit(
            RegistryEventType.INITIATIVE_UPDATED,
            updated,
            before=current.status,
            prior_revision=current.current_revision,
        )
        return updated

    def get_revision(self, initiative_id: str, revision: int) -> ConfigurationRevision:
        self.get(initiative_id)
        found = self._repository.find_revision(initiative_id, revision)
        if found is None or found.initiative_id != initiative_id:
            raise InitiativeRevisionNotFoundError(initiative_id, revision)
        return found

    def list_revisions(self, initiative_id: str) -> tuple[ConfigurationRevision, ...]:
        self.get(initiative_id)
        return tuple(
            sorted(self._repository.list_revisions(initiative_id), key=lambda item: item.revision)
        )

    def rollback(self, initiative_id: str, target_revision: int) -> RegisteredInitiative:
        current = self.get(initiative_id)
        source = self.get_revision(initiative_id, target_revision)
        if target_revision == current.current_revision:
            return current

        now = self._now()
        new_number = current.current_revision + 1
        revision = ConfigurationRevision(
            initiative_id,
            new_number,
            source.profile,
            now,
            ConfigurationRevisionReason.ROLLBACK,
            source_revision=target_revision,
        )
        rolled_back = replace(
            current, profile=source.profile, current_revision=new_number, updated_at=now
        )
        self._repository.append_revision(rolled_back, revision, expected_current=current)
        self._emit(
            RegistryEventType.INITIATIVE_ROLLED_BACK,
            rolled_back,
            before=current.status,
            prior_revision=current.current_revision,
            source_revision=target_revision,
        )
        return rolled_back

    def disable(self, initiative_id: str) -> RegisteredInitiative:
        """Disable once; subsequent calls return the same record without another event."""
        current = self.get(initiative_id)
        if current.status == InitiativeStatus.DISABLED:
            return current
        disabled = replace(current, status=InitiativeStatus.DISABLED, updated_at=self._now())
        self._repository.replace_if_current(disabled, expected_current=current)
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
        prior_revision: int | None = None,
        source_revision: int | None = None,
    ) -> None:
        metadata = {
            "schema_version": registered.profile.schema_version,
            "status_before": before.value if before is not None else "unregistered",
            "status_after": registered.status.value,
            "revision": str(registered.current_revision),
        }
        if prior_revision is not None:
            metadata["prior_revision"] = str(prior_revision)
        if source_revision is not None:
            metadata["source_revision"] = str(source_revision)
        event = RegistryMutationEvent(
            event_type=event_type,
            initiative_id=registered.id,
            occurred_at=registered.updated_at,
            metadata=metadata,
        )
        self._event_sink.publish(event)
