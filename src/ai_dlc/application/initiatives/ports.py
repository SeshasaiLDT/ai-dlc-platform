"""Initiative-specific storage and audit boundaries."""

from typing import Protocol

from .models import ConfigurationRevision, RegisteredInitiative, RegistryMutationEvent


class InitiativeRepository(Protocol):
    """Store current registrations; insert and replace must be atomic per ID."""

    def insert(self, initiative: RegisteredInitiative) -> None:
        """Add a new ID or raise InitiativeAlreadyExistsError."""

    def find_by_id(self, initiative_id: str) -> RegisteredInitiative | None:
        """Return the current registration, if present."""

    def list_all(self) -> tuple[RegisteredInitiative, ...]:
        """Return a snapshot of all current registrations."""

    def replace(self, initiative: RegisteredInitiative) -> None:
        """Replace lifecycle metadata only; profile changes require a new revision."""


class InitiativeRevisionRepository(InitiativeRepository, Protocol):
    """Atomically maintain current registration and append-only configuration history."""

    def insert_with_revision(
        self, initiative: RegisteredInitiative, revision: ConfigurationRevision
    ) -> None:
        """Create a registration and its first revision together."""

    def append_revision(
        self,
        initiative: RegisteredInitiative,
        revision: ConfigurationRevision,
        *,
        expected_current: RegisteredInitiative,
    ) -> None:
        """Append next revision and update current record if expected state still matches."""

    def find_revision(self, initiative_id: str, revision: int) -> ConfigurationRevision | None:
        """Find an immutable historical snapshot."""

    def list_revisions(self, initiative_id: str) -> tuple[ConfigurationRevision, ...]:
        """Return an ascending immutable snapshot of history."""

    def replace_if_current(
        self, initiative: RegisteredInitiative, *, expected_current: RegisteredInitiative
    ) -> None:
        """Replace lifecycle metadata only when the observed record remains current."""


class RegistryEventSink(Protocol):
    """Receive successful Registry mutation events."""

    def publish(self, event: RegistryMutationEvent) -> None:
        """Record one mutation event."""
