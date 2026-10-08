"""Governed model deployment registry (metadata, role eligibility, enablement, audit)."""

from .eligibility import EligibilityResult, IneligibleReason, evaluate_eligibility
from .errors import (
    DeploymentExistsError,
    DeploymentNotFoundError,
    ModelRegistryAuditError,
    ModelRegistryError,
    RevisionConflictError,
)
from .models import (
    DeploymentCapabilities,
    DeploymentContext,
    DeploymentGovernance,
    LatencyMetadata,
    ModelDeploymentSpec,
    ModelRegistryAuditEvent,
    OperationalAvailability,
    PricingMetadata,
    RegisteredModel,
    RegistryOperation,
)
from .ports import ModelRegistryRepository
from .service import ModelRegistryAdmin, ModelRegistryReader

__all__ = [
    "DeploymentCapabilities",
    "DeploymentContext",
    "DeploymentExistsError",
    "DeploymentGovernance",
    "DeploymentNotFoundError",
    "EligibilityResult",
    "IneligibleReason",
    "LatencyMetadata",
    "ModelDeploymentSpec",
    "ModelRegistryAdmin",
    "ModelRegistryAuditError",
    "ModelRegistryAuditEvent",
    "ModelRegistryError",
    "ModelRegistryReader",
    "ModelRegistryRepository",
    "OperationalAvailability",
    "PricingMetadata",
    "RegisteredModel",
    "RegistryOperation",
    "RevisionConflictError",
    "evaluate_eligibility",
]
