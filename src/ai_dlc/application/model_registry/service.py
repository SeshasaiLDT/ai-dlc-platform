"""Read-only and administrative model registry services.

Reads are separate from administration: a ``ModelRegistryReader`` has no mutation methods and
needs no privilege. Every mutation requires a trusted, authenticated ``Principal`` that passes
the existing ``AuthorizationService`` platform-administration check (audited by that service).
Nothing here invokes a model, ranks candidates or selects a deployment.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ai_dlc.application.agent_harness import ModelSelectionRequest, RoleProfiles
from ai_dlc.application.authorization import AuthorizationService
from ai_dlc.application.authorization.decisions import PlatformAuthorizationRequest
from ai_dlc.domain.identity import Principal

from .eligibility import EligibilityResult, evaluate_eligibility
from .errors import (
    DeploymentNotFoundError,
    ModelRegistryAuditError,
    ModelRegistryError,
    RevisionConflictError,
)
from .models import (
    ModelDeploymentSpec,
    ModelRegistryAuditEvent,
    OperationalAvailability,
    RegisteredModel,
    RegistryOperation,
    utc_now,
    validate_correlation_id,
)
from .ports import ModelRegistryRepository

_STATE_FIELDS = ("enabled", "availability")


class ModelRegistryReader:
    """Safe read interface for future routing. Always reads through; it caches nothing."""

    def __init__(self, repository: ModelRegistryRepository) -> None:
        self._repository = repository

    def get(self, deployment_id: str) -> RegisteredModel:
        record = self._repository.get(deployment_id)
        if record is None:
            raise DeploymentNotFoundError("deployment not found")
        return record

    def list(self, *, enabled_only: bool = False) -> tuple[RegisteredModel, ...]:
        return tuple(r for r in self._repository.list_all() if r.enabled or not enabled_only)

    def evaluate(
        self, request: ModelSelectionRequest, profiles: RoleProfiles
    ) -> tuple[EligibilityResult, ...]:
        """Per-deployment eligibility with reasons, ordered by deployment ID (not ranked)."""
        requirements = request.effective_requirements(profiles.for_role(request.role))
        return tuple(
            evaluate_eligibility(
                model,
                requirements,
                data_classification=request.data_classification,
                separate_from=request.separate_from_deployments,
            )
            for model in self._repository.list_all()
        )

    def query_eligible(
        self, request: ModelSelectionRequest, profiles: RoleProfiles
    ) -> tuple[RegisteredModel, ...]:
        """Deployments passing the request's effective requirements, ordered by ID."""
        eligible = {r.deployment_id for r in self.evaluate(request, profiles) if r.eligible}
        return tuple(m for m in self._repository.list_all() if m.deployment_id in eligible)


class ModelRegistryAdmin:
    def __init__(
        self,
        repository: ModelRegistryRepository,
        authorizer: AuthorizationService,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._authorizer = authorizer
        self._clock = clock

    def register(
        self, principal: Principal, spec: ModelDeploymentSpec, *, correlation_id: str
    ) -> RegisteredModel:
        """New deployments start disabled; enabling is a separate, audited step."""
        decision_id = self._authorize(principal, correlation_id)
        self._require_spec(spec)
        record = RegisteredModel(spec=spec, enabled=False, revision=1, updated_at=self._now())
        self._commit(
            principal, RegistryOperation.REGISTER, None, record, None,
            (*ModelDeploymentSpec.model_fields, *_STATE_FIELDS), correlation_id, decision_id,
        )  # fmt: skip
        return record

    def update_metadata(
        self,
        principal: Principal,
        spec: ModelDeploymentSpec,
        *,
        expected_revision: int,
        correlation_id: str,
    ) -> RegisteredModel:
        decision_id = self._authorize(principal, correlation_id)
        self._require_spec(spec)
        current = self._current(spec.deployment_id, expected_revision)
        changed = tuple(
            name
            for name in type(spec).model_fields
            if getattr(spec, name) != getattr(current.spec, name)
        )
        if not changed:
            return current
        updated = RegisteredModel(
            spec=spec,
            enabled=current.enabled,
            availability=current.availability,
            revision=current.revision + 1,
            updated_at=self._now(),
        )
        self._commit(
            principal, RegistryOperation.UPDATE_METADATA, current.revision, updated,
            expected_revision, changed, correlation_id, decision_id,
        )  # fmt: skip
        return updated

    def enable(
        self,
        principal: Principal,
        deployment_id: str,
        *,
        expected_revision: int,
        correlation_id: str,
    ) -> RegisteredModel:
        return self._set_enabled(principal, deployment_id, True, expected_revision, correlation_id)

    def disable(
        self,
        principal: Principal,
        deployment_id: str,
        *,
        expected_revision: int,
        correlation_id: str,
    ) -> RegisteredModel:
        return self._set_enabled(principal, deployment_id, False, expected_revision, correlation_id)

    def set_availability(
        self,
        principal: Principal,
        deployment_id: str,
        availability: OperationalAvailability,
        *,
        expected_revision: int,
        correlation_id: str,
    ) -> RegisteredModel:
        """Record an operational state; it never changes the administrative enabled flag."""
        decision_id = self._authorize(principal, correlation_id)
        availability = OperationalAvailability(availability)
        current = self._current(deployment_id, expected_revision)
        if current.availability is availability:
            return current
        updated = RegisteredModel(
            spec=current.spec,
            enabled=current.enabled,
            availability=availability,
            revision=current.revision + 1,
            updated_at=self._now(),
        )
        self._commit(
            principal, RegistryOperation.UPDATE_AVAILABILITY, current.revision, updated,
            expected_revision, ("availability",), correlation_id, decision_id,
        )  # fmt: skip
        return updated

    def _set_enabled(
        self,
        principal: Principal,
        deployment_id: str,
        enabled: bool,
        expected_revision: int,
        correlation_id: str,
    ) -> RegisteredModel:
        decision_id = self._authorize(principal, correlation_id)
        current = self._current(deployment_id, expected_revision)
        if current.enabled is enabled:
            return current  # deterministic and idempotent: no change, no revision, no audit
        updated = RegisteredModel(
            spec=current.spec,
            enabled=enabled,
            availability=current.availability,
            revision=current.revision + 1,
            updated_at=self._now(),
        )
        operation = RegistryOperation.ENABLE if enabled else RegistryOperation.DISABLE
        self._commit(
            principal, operation, current.revision, updated, expected_revision,
            ("enabled",), correlation_id, decision_id,
        )  # fmt: skip
        return updated

    def _authorize(self, principal: Principal, correlation_id: str) -> str:
        validate_correlation_id(correlation_id)
        # Raises TypeError for a non-Principal and AuthorizationDeniedError when not an admin.
        return self._authorizer.require(PlatformAuthorizationRequest(principal)).decision_id

    @staticmethod
    def _require_spec(spec: ModelDeploymentSpec) -> None:
        if not isinstance(spec, ModelDeploymentSpec):
            raise TypeError("spec must be a ModelDeploymentSpec")

    def _current(self, deployment_id: str, expected_revision: int) -> RegisteredModel:
        current = self._repository.get(deployment_id)
        if current is None:
            raise DeploymentNotFoundError("deployment not found")
        if type(expected_revision) is not int or current.revision != expected_revision:
            raise RevisionConflictError("stale revision")
        return current

    def _commit(
        self,
        principal: Principal,
        operation: RegistryOperation,
        previous_revision: int | None,
        record: RegisteredModel,
        expected_revision: int | None,
        changed_fields: tuple[str, ...],
        correlation_id: str,
        decision_id: str,
    ) -> None:
        event = ModelRegistryAuditEvent(
            deployment_id=record.deployment_id,
            operation=operation,
            actor_id=principal.subject_id,
            occurred_at=record.updated_at,
            previous_revision=previous_revision,
            new_revision=record.revision,
            changed_fields=changed_fields,
            correlation_id=correlation_id,
            authorization_decision_id=decision_id,
        )
        try:
            self._repository.commit(record, event, expected_revision=expected_revision)
        except ModelRegistryError:
            raise
        except Exception:
            raise ModelRegistryAuditError("registry commit failed; change not applied") from None

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise ValueError("registry clock must be timezone-aware")
        return value.astimezone(UTC)
