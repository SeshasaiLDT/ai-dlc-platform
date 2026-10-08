"""Reviewer independence rules and read-only candidate filtering (no ranking, no fallback)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ai_dlc.application.agent_harness import ModelRole, ModelSelectionRequest, RoleProfiles
from ai_dlc.application.model_registry import ModelRegistryReader, RegisteredModel
from ai_dlc.domain.initiative.models import ReviewPolicy

from .models import ModelProvenance


class IndependenceViolation(StrEnum):
    SAME_DEPLOYMENT = "same_deployment"
    SAME_MODEL = "same_model"
    SAME_FAMILY = "same_family"
    SAME_PROVIDER = "same_provider"
    UNKNOWN_FAMILY = "unknown_family"
    UNKNOWN_GENERATOR = "unknown_generator"


@dataclass(frozen=True, slots=True)
class ReviewRequirements:
    """Effective independence requirements: the policy, possibly tightened by a request."""

    require_different_deployment: bool
    require_different_model: bool
    require_different_family: bool
    require_different_provider: bool
    allow_unknown_provenance: bool

    @classmethod
    def from_policy(
        cls,
        policy: ReviewPolicy,
        *,
        tighten_deployment: bool = False,
        tighten_model: bool = False,
        tighten_family: bool = False,
        tighten_provider: bool = False,
        forbid_unknown_provenance: bool = False,
    ) -> ReviewRequirements:
        """A request can only turn requirements on (or forbid waivers), never off."""
        return cls(
            policy.require_different_deployment or tighten_deployment,
            policy.require_different_model or tighten_model,
            policy.require_different_family or tighten_family,
            policy.require_different_provider or tighten_provider,
            policy.allow_unknown_provenance and not forbid_unknown_provenance,
        )


def check_independence(
    generator: ModelProvenance | None,
    reviewer: ModelProvenance,
    requirements: ReviewRequirements,
) -> tuple[IndependenceViolation, ...]:
    """Violations of the required separations; unknown information fails closed.

    Deployment IDs, model identifiers, families and providers are compared as separate
    explicit fields. Nothing is inferred from name strings.
    """
    needs_generator = any(
        (
            requirements.require_different_deployment,
            requirements.require_different_model,
            requirements.require_different_family,
            requirements.require_different_provider,
        )
    )
    if generator is None:
        if needs_generator and not requirements.allow_unknown_provenance:
            return (IndependenceViolation.UNKNOWN_GENERATOR,)
        return ()
    found: list[IndependenceViolation] = []
    if requirements.require_different_deployment and (
        generator.deployment_id == reviewer.deployment_id
    ):
        found.append(IndependenceViolation.SAME_DEPLOYMENT)
    if requirements.require_different_model and (
        generator.model_identifier == reviewer.model_identifier
    ):
        found.append(IndependenceViolation.SAME_MODEL)
    if requirements.require_different_provider and generator.provider_id == reviewer.provider_id:
        found.append(IndependenceViolation.SAME_PROVIDER)
    if requirements.require_different_family:
        if generator.model_family is None or reviewer.model_family is None:
            if not requirements.allow_unknown_provenance:
                found.append(IndependenceViolation.UNKNOWN_FAMILY)
        elif generator.model_family == reviewer.model_family:
            found.append(IndependenceViolation.SAME_FAMILY)
    return tuple(found)


@dataclass(frozen=True, slots=True)
class ReviewerCandidates:
    eligible: tuple[RegisteredModel, ...]
    rejected: tuple[tuple[str, tuple[str, ...]], ...]  # (deployment_id, reason codes)


def eligible_reviewers(
    registry: ModelRegistryReader,
    request: ModelSelectionRequest,
    profiles: RoleProfiles,
    generator: ModelProvenance | None,
    requirements: ReviewRequirements,
    *,
    execution_ref: str = "candidate-check",
) -> ReviewerCandidates:
    """Registry-eligible reviewer deployments that also satisfy independence. Ordered by ID."""
    if request.role is not ModelRole.INDEPENDENT_REVIEWER:
        raise ValueError("reviewer selection requires the independent reviewer role")
    if requirements.require_different_deployment and generator is not None:
        request = request.model_copy(
            update={
                "separate_from_deployments": request.separate_from_deployments
                | {generator.deployment_id}
            }
        )
    by_id = {m.deployment_id: m for m in registry.list()}
    eligible: list[RegisteredModel] = []
    rejected: list[tuple[str, tuple[str, ...]]] = []
    for result in registry.evaluate(request, profiles):
        if not result.eligible:
            rejected.append((result.deployment_id, tuple(r.value for r in result.reasons)))
            continue
        model = by_id[result.deployment_id]
        provenance = ModelProvenance.from_registry(
            model, role=ModelRole.INDEPENDENT_REVIEWER, execution_ref=execution_ref
        )
        violations = check_independence(generator, provenance, requirements)
        if violations:
            rejected.append((result.deployment_id, tuple(v.value for v in violations)))
        else:
            eligible.append(model)
    return ReviewerCandidates(tuple(eligible), tuple(rejected))
