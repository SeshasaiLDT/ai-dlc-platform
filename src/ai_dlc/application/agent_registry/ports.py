"""Persistence boundary for agent manifests."""

from typing import Protocol

from .models import RegisteredAgent


class AgentRegistryRepository(Protocol):
    def get(self, agent_id: str) -> RegisteredAgent | None: ...

    def list_all(self) -> tuple[RegisteredAgent, ...]: ...

    def put(self, record: RegisteredAgent, *, expected_revision: int | None) -> None: ...


class AgentRegistryReader(Protocol):
    def get(self, agent_id: str) -> RegisteredAgent | None: ...

    def discover(
        self,
        *,
        required_capabilities: frozenset[str] = frozenset(),
        allowed_permissions: frozenset[str] = frozenset(),
        include_unhealthy: bool = False,
    ) -> tuple[RegisteredAgent, ...]: ...
