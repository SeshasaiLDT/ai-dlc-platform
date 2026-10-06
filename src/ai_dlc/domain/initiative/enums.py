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
