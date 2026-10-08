"""Deterministic in-memory registry store for tests and local use.

Atomicity here is process-local (one lock). It does NOT demonstrate cross-runtime configuration
sharing; that requires a durable adapter implementing the same port.
"""

from collections.abc import Callable
from threading import Lock

from ai_dlc.application.model_registry.errors import (
    DeploymentExistsError,
    DeploymentNotFoundError,
    ModelRegistryAuditError,
    RevisionConflictError,
)
from ai_dlc.application.model_registry.models import ModelRegistryAuditEvent, RegisteredModel


class InMemoryModelRegistryRepository:
    def __init__(
        self, *, audit_hook: Callable[[ModelRegistryAuditEvent], None] | None = None
    ) -> None:
        self._records: dict[str, RegisteredModel] = {}
        self._events: list[ModelRegistryAuditEvent] = []
        self._lock = Lock()
        self._audit_hook = audit_hook

    def get(self, deployment_id: str) -> RegisteredModel | None:
        with self._lock:
            return self._records.get(deployment_id)

    def list_all(self) -> tuple[RegisteredModel, ...]:
        with self._lock:
            return tuple(self._records[key] for key in sorted(self._records))

    def commit(
        self,
        record: RegisteredModel,
        event: ModelRegistryAuditEvent,
        *,
        expected_revision: int | None,
    ) -> None:
        if event.deployment_id != record.deployment_id or event.new_revision != record.revision:
            raise ValueError("audit event does not describe the record")
        with self._lock:
            current = self._records.get(record.deployment_id)
            if expected_revision is None:
                if current is not None:
                    raise DeploymentExistsError("deployment already registered")
            elif current is None:
                raise DeploymentNotFoundError("deployment not found")
            elif current.revision != expected_revision:
                raise RevisionConflictError("stale revision")
            try:
                if self._audit_hook is not None:
                    self._audit_hook(event)
            except Exception:
                raise ModelRegistryAuditError(
                    "audit event not recorded; change not applied"
                ) from None
            self._events.append(event)
            self._records[record.deployment_id] = record

    def audit_events(self, deployment_id: str | None = None) -> tuple[ModelRegistryAuditEvent, ...]:
        with self._lock:
            return tuple(
                e for e in self._events if deployment_id is None or e.deployment_id == deployment_id
            )
