"""Process-local agent registry for tests and local development."""

from threading import Lock

from ai_dlc.application.agent_registry.models import RegisteredAgent


class InMemoryAgentRegistryRepository:
    def __init__(self) -> None:
        self._records: dict[str, RegisteredAgent] = {}
        self._lock = Lock()

    def get(self, agent_id: str) -> RegisteredAgent | None:
        with self._lock:
            return self._records.get(agent_id)

    def list_all(self) -> tuple[RegisteredAgent, ...]:
        with self._lock:
            return tuple(self._records[key] for key in sorted(self._records))

    def put(self, record: RegisteredAgent, *, expected_revision: int | None) -> None:
        with self._lock:
            current = self._records.get(record.manifest.agent_id)
            if (current.revision if current else None) != expected_revision:
                raise ValueError("agent registry revision conflict")
            self._records[record.manifest.agent_id] = record
