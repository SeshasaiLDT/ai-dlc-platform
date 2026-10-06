"""Local and test implementations of the Initiative Registry ports."""

from threading import Lock

from ai_dlc.application.initiatives.errors import (
    InitiativeAlreadyExistsError,
    InitiativeNotFoundError,
    InitiativeRevisionConflictError,
)
from ai_dlc.application.initiatives.models import (
    ConfigurationRevision,
    ConfigurationRevisionReason,
    RegisteredInitiative,
    RegistryMutationEvent,
)


class InMemoryInitiativeRepository:
    def __init__(self) -> None:
        self._records: dict[str, RegisteredInitiative] = {}
        self._revisions: dict[str, list[ConfigurationRevision]] = {}
        self._lock = Lock()

    def insert(self, initiative: RegisteredInitiative) -> None:
        first = ConfigurationRevision(
            initiative_id=initiative.id,
            revision=1,
            profile=initiative.profile,
            created_at=initiative.created_at,
            reason=ConfigurationRevisionReason.CREATED,
        )
        self.insert_with_revision(initiative, first)

    def insert_with_revision(
        self, initiative: RegisteredInitiative, revision: ConfigurationRevision
    ) -> None:
        with self._lock:
            if initiative.id in self._records:
                raise InitiativeAlreadyExistsError(initiative.id)
            if (
                revision.initiative_id != initiative.id
                or revision.revision != 1
                or revision.reason != ConfigurationRevisionReason.CREATED
                or revision.profile != initiative.profile
                or initiative.current_revision != 1
                or revision.created_at != initiative.created_at
            ):
                raise InitiativeRevisionConflictError(initiative.id)
            self._records[initiative.id] = initiative
            self._revisions[initiative.id] = [revision]

    def find_by_id(self, initiative_id: str) -> RegisteredInitiative | None:
        with self._lock:
            return self._records.get(initiative_id)

    def list_all(self) -> tuple[RegisteredInitiative, ...]:
        with self._lock:
            return tuple(self._records.values())

    def replace(self, initiative: RegisteredInitiative) -> None:
        with self._lock:
            current = self._records.get(initiative.id)
            if current is None:
                raise InitiativeNotFoundError(initiative.id)
            if (
                initiative.current_revision != current.current_revision
                or initiative.profile != current.profile
            ):
                raise InitiativeRevisionConflictError(initiative.id)
            self._records[initiative.id] = initiative

    def replace_if_current(
        self, initiative: RegisteredInitiative, *, expected_current: RegisteredInitiative
    ) -> None:
        with self._lock:
            current = self._records.get(initiative.id)
            if current is None:
                raise InitiativeNotFoundError(initiative.id)
            if (
                current != expected_current
                or initiative.current_revision != current.current_revision
                or initiative.profile != current.profile
            ):
                raise InitiativeRevisionConflictError(initiative.id)
            self._records[initiative.id] = initiative

    def append_revision(
        self,
        initiative: RegisteredInitiative,
        revision: ConfigurationRevision,
        *,
        expected_current: RegisteredInitiative,
    ) -> None:
        with self._lock:
            current = self._records.get(initiative.id)
            if current is None:
                raise InitiativeNotFoundError(initiative.id)
            latest = self._revisions[initiative.id][-1]
            if (
                current != expected_current
                or latest.revision != expected_current.current_revision
                or revision.initiative_id != initiative.id
                or revision.revision != latest.revision + 1
                or revision.reason == ConfigurationRevisionReason.CREATED
                or initiative.current_revision != revision.revision
                or initiative.profile != revision.profile
                or initiative.created_at != current.created_at
                or initiative.status != current.status
                or initiative.updated_at != revision.created_at
                or (
                    revision.reason == ConfigurationRevisionReason.ROLLBACK
                    and (
                        revision.source_revision > len(self._revisions[initiative.id])
                        or revision.profile
                        != self._revisions[initiative.id][revision.source_revision - 1].profile
                    )
                )
            ):
                raise InitiativeRevisionConflictError(initiative.id)
            self._revisions[initiative.id].append(revision)
            self._records[initiative.id] = initiative

    def find_revision(self, initiative_id: str, revision: int) -> ConfigurationRevision | None:
        with self._lock:
            history = self._revisions.get(initiative_id)
            if history is None or type(revision) is not int or revision < 1:
                return None
            return history[revision - 1] if revision <= len(history) else None

    def list_revisions(self, initiative_id: str) -> tuple[ConfigurationRevision, ...]:
        with self._lock:
            history = self._revisions.get(initiative_id)
            if history is None:
                raise InitiativeNotFoundError(initiative_id)
            return tuple(sorted(history, key=lambda item: item.revision))


class InMemoryRegistryEventSink:
    def __init__(self) -> None:
        self._events: list[RegistryMutationEvent] = []
        self._lock = Lock()

    def publish(self, event: RegistryMutationEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[RegistryMutationEvent, ...]:
        with self._lock:
            return tuple(self._events)
