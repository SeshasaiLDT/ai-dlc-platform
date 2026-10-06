"""Local and test implementations of the Initiative Registry ports."""

from threading import Lock

from ai_dlc.application.initiatives.errors import (
    InitiativeAlreadyExistsError,
    InitiativeNotFoundError,
)
from ai_dlc.application.initiatives.models import RegisteredInitiative, RegistryMutationEvent


class InMemoryInitiativeRepository:
    def __init__(self) -> None:
        self._records: dict[str, RegisteredInitiative] = {}
        self._lock = Lock()

    def insert(self, initiative: RegisteredInitiative) -> None:
        with self._lock:
            if initiative.id in self._records:
                raise InitiativeAlreadyExistsError(initiative.id)
            self._records[initiative.id] = initiative

    def find_by_id(self, initiative_id: str) -> RegisteredInitiative | None:
        with self._lock:
            return self._records.get(initiative_id)

    def list_all(self) -> tuple[RegisteredInitiative, ...]:
        with self._lock:
            return tuple(self._records.values())

    def replace(self, initiative: RegisteredInitiative) -> None:
        with self._lock:
            if initiative.id not in self._records:
                raise InitiativeNotFoundError(initiative.id)
            self._records[initiative.id] = initiative


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
