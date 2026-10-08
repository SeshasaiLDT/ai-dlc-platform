"""Narrow persistence port for the model registry."""

from typing import Protocol

from .models import ModelRegistryAuditEvent, RegisteredModel


class ModelRegistryRepository(Protocol):
    """Shared, durable configuration store.

    ``commit`` is the only write. A durable adapter MUST apply the record change and append the
    audit event in one atomic unit (single transaction or conditional batch write), and MUST
    compare ``expected_revision`` against the stored revision inside that same unit. If it cannot
    guarantee that, it must not claim to implement this port.
    """

    def get(self, deployment_id: str) -> RegisteredModel | None:
        """Return the current record; reads must reflect committed changes from any runtime."""

    def list_all(self) -> tuple[RegisteredModel, ...]:
        """Return a consistent snapshot of current records."""

    def commit(
        self,
        record: RegisteredModel,
        event: ModelRegistryAuditEvent,
        *,
        expected_revision: int | None,
    ) -> None:
        """Store ``record`` and ``event`` atomically.

        ``expected_revision=None`` means create: raise DeploymentExistsError if present.
        Otherwise raise RevisionConflictError (or DeploymentNotFoundError) and change nothing
        unless the stored revision equals ``expected_revision``. If the audit event cannot be
        recorded raise ModelRegistryAuditError and change nothing.
        """

    def audit_events(self, deployment_id: str | None = None) -> tuple[ModelRegistryAuditEvent, ...]:
        """Return audit events in commit order, optionally for one deployment."""
