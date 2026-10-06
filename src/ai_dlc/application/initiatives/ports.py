"""Initiative-specific storage and audit boundaries."""

from typing import Protocol

from .models import RegisteredInitiative, RegistryMutationEvent


class InitiativeRepository(Protocol):
    """Store current registrations; insert and replace must be atomic per ID."""

    def insert(self, initiative: RegisteredInitiative) -> None:
        """Add a new ID or raise InitiativeAlreadyExistsError."""

    def find_by_id(self, initiative_id: str) -> RegisteredInitiative | None:
        """Return the current registration, if present."""

    def list_all(self) -> tuple[RegisteredInitiative, ...]:
        """Return a snapshot of all current registrations."""

    def replace(self, initiative: RegisteredInitiative) -> None:
        """Replace an existing ID or raise InitiativeNotFoundError."""


class RegistryEventSink(Protocol):
    """Receive successful Registry mutation events."""

    def publish(self, event: RegistryMutationEvent) -> None:
        """Record one mutation event."""
