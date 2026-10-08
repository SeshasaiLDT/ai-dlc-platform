"""Constrained Initiative Profile values."""

from enum import StrEnum


class GitProvider(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"


class RepositoryAccess(StrEnum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class KnowledgeSourceType(StrEnum):
    DOCUMENTATION = "documentation"
    REPOSITORY = "repository"
    TICKETING = "ticketing"
    INCIDENT = "incident"


class ModelRole(StrEnum):
    ROUTING = "routing"
    STANDARD_REASONING = "standard_reasoning"
    DEEP_REASONING = "deep_reasoning"
    INDEPENDENT_REVIEWER = "independent_reviewer"


class BudgetPeriod(StrEnum):
    DAILY = "daily"
    MONTHLY = "monthly"


class BudgetAction(StrEnum):
    BLOCK = "block"
    REQUIRE_APPROVAL = "require_approval"
    ALLOW = "allow"  # audit-only: record the breach but still admit


class FallbackFailure(StrEnum):
    """Failure categories an initiative may permit fallback for (others are never eligible)."""

    TRANSIENT = "transient"
    THROTTLED = "throttled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PERMANENT_MODEL_ERROR = "permanent_model_error"
    INVALID_OUTPUT = "invalid_output"
    CONTEXT_CAPACITY = "context_capacity"


class FallbackOrdering(StrEnum):
    PRIORITY = "priority"
    COST = "cost"
    LATENCY = "latency"
