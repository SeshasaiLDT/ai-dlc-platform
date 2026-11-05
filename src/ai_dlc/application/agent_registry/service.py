"""Manifest registration and policy-filtered discovery."""

from datetime import UTC, datetime

from .models import AgentManifest, RegisteredAgent
from .ports import AgentRegistryRepository


class AgentRegistry:
    def __init__(self, repository: AgentRegistryRepository, *, clock=lambda: datetime.now(UTC)):
        self._repository = repository
        self._clock = clock

    def register(self, manifest: AgentManifest) -> RegisteredAgent:
        if not isinstance(manifest, AgentManifest):
            raise TypeError("AgentManifest required")
        current = self._repository.get(manifest.agent_id)
        revision = current.revision + 1 if current else 1
        record = RegisteredAgent(
            manifest=manifest, revision=revision, updated_at=self._clock(), enabled=True
        )
        self._repository.put(record, expected_revision=current.revision if current else None)
        return record

    def get(self, agent_id: str) -> RegisteredAgent | None:
        return self._repository.get(agent_id)

    def discover(
        self,
        *,
        required_capabilities: frozenset[str] = frozenset(),
        allowed_permissions: frozenset[str] = frozenset(),
        include_unhealthy: bool = False,
    ) -> tuple[RegisteredAgent, ...]:
        """Return enabled manifests satisfying policy, in deterministic ID order."""
        return tuple(
            record
            for record in self._repository.list_all()
            if record.enabled
            and required_capabilities <= frozenset(record.manifest.capabilities)
            and record.manifest.permissions <= allowed_permissions
            and (include_unhealthy or record.manifest.health.status not in {"unhealthy"})
        )
