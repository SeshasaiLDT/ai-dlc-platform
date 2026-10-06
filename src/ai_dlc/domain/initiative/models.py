"""Version 1.0 of the initiative-owned, secret-free configuration contract."""

import re
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .enums import GitProvider, KnowledgeSourceType, ModelRole, RepositoryAccess

CURRENT_SCHEMA_VERSION = "1.0"

Identifier = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=80)
]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
JiraProjectKey = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=20)]


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _HasId(Protocol):
    id: str


def _unique_ids[T: _HasId](items: tuple[T, ...], field: str) -> tuple[T, ...]:
    seen: set[str] = set()
    for item in items:
        identifier = item.id
        if identifier in seen:
            raise ValueError(f"duplicate {field} id '{identifier}'")
        seen.add(identifier)
    return items


class InitiativeIdentity(ProfileModel):
    id: Identifier
    name: NonBlank
    description: str | None = None


class Team(ProfileModel):
    id: Identifier
    name: NonBlank
    description: str | None = None


class Ownership(ProfileModel):
    primary_team_id: Identifier
    supporting_team_ids: tuple[Identifier, ...] = ()

    @field_validator("supporting_team_ids")
    @classmethod
    def unique_supporting_teams(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("supporting team ids must be unique")
        return value


class JiraIntegration(ProfileModel):
    enabled: bool = False
    projects: tuple[JiraProjectKey, ...] = ()

    @model_validator(mode="after")
    def enabled_has_projects(self) -> Self:
        if self.enabled and not self.projects:
            raise ValueError("enabled Jira integration requires at least one project key")
        if len(self.projects) != len(set(self.projects)):
            raise ValueError("Jira project keys must be unique")
        return self


class ServiceNowIntegration(ProfileModel):
    enabled: bool = False
    assignment_groups: tuple[NonBlank, ...] = ()
    scopes: tuple[NonBlank, ...] = ()


class GitRepository(ProfileModel):
    id: Identifier
    owner: NonBlank | None = None
    name: NonBlank
    default_branch: NonBlank
    access: RepositoryAccess = RepositoryAccess.READ_ONLY


class GitIntegration(ProfileModel):
    enabled: bool = False
    provider: GitProvider | None = None
    repositories: tuple[GitRepository, ...] = ()

    @field_validator("repositories")
    @classmethod
    def unique_repositories(cls, value: tuple[GitRepository, ...]) -> tuple[GitRepository, ...]:
        return _unique_ids(value, "repository")

    @model_validator(mode="after")
    def enabled_has_provider_and_repositories(self) -> Self:
        if self.enabled and (self.provider is None or not self.repositories):
            raise ValueError(
                "enabled Git integration requires a provider and at least one repository"
            )
        if self.provider == GitProvider.GITHUB:
            for repository in self.repositories:
                if repository.owner is None:
                    raise ValueError(f"GitHub repository '{repository.id}' requires an owner")
        return self


class Integrations(ProfileModel):
    jira: JiraIntegration = Field(default_factory=JiraIntegration)
    servicenow: ServiceNowIntegration = Field(default_factory=ServiceNowIntegration)
    git: GitIntegration = Field(default_factory=GitIntegration)


class KnowledgeSource(ProfileModel):
    id: Identifier
    type: KnowledgeSourceType
    enabled: bool = True
    resource_id: NonBlank | None = None
    description: str | None = None


class Knowledge(ProfileModel):
    sources: tuple[KnowledgeSource, ...] = ()

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value: tuple[KnowledgeSource, ...]) -> tuple[KnowledgeSource, ...]:
        return _unique_ids(value, "knowledge source")


class BuildProfile(ProfileModel):
    id: Identifier
    repository_id: Identifier | None = None
    language: NonBlank
    runtime: NonBlank | None = None
    working_directory: NonBlank = "."
    install_command: NonBlank | None = None
    build_command: NonBlank | None = None
    test_command: NonBlank | None = None

    @field_validator("working_directory")
    @classmethod
    def relative_working_directory(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or ".." in value.split("/")
            or re.match(r"^[A-Za-z]:", value)
        ):
            raise ValueError("must be a relative repository path without '..' segments")
        return value


class OperationPolicy(ProfileModel):
    enabled: bool = False
    human_approval_required: bool = False


class Policies(ProfileModel):
    code_analysis: OperationPolicy = Field(default_factory=OperationPolicy)
    code_generation: OperationPolicy = Field(default_factory=OperationPolicy)
    git_write: OperationPolicy = Field(default_factory=OperationPolicy)
    servicenow_write: OperationPolicy = Field(default_factory=OperationPolicy)
    jira_write: OperationPolicy = Field(default_factory=OperationPolicy)


class ModelRoleDefaults(ProfileModel):
    routing: ModelRole = ModelRole.ROUTING
    reasoning: ModelRole = ModelRole.STANDARD_REASONING
    complex_reasoning: ModelRole = ModelRole.DEEP_REASONING
    reviewer: ModelRole = ModelRole.INDEPENDENT_REVIEWER


class Defaults(ProfileModel):
    model_roles: ModelRoleDefaults = Field(default_factory=ModelRoleDefaults)


class InitiativeProfile(ProfileModel):
    schema_version: Literal["1.0"]
    initiative: InitiativeIdentity
    teams: tuple[Team, ...] = Field(min_length=1)
    ownership: Ownership
    integrations: Integrations = Field(default_factory=Integrations)
    knowledge: Knowledge = Field(default_factory=Knowledge)
    build_profiles: tuple[BuildProfile, ...] = ()
    policies: Policies = Field(default_factory=Policies)
    defaults: Defaults = Field(default_factory=Defaults)

    @field_validator("schema_version", mode="before")
    @classmethod
    def supported_version(cls, value: object) -> object:
        if value != CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema version '{value}'; supported version: {CURRENT_SCHEMA_VERSION}"
            )
        return value

    @field_validator("teams")
    @classmethod
    def unique_teams(cls, value: tuple[Team, ...]) -> tuple[Team, ...]:
        return _unique_ids(value, "team")

    @field_validator("build_profiles")
    @classmethod
    def unique_build_profiles(cls, value: tuple[BuildProfile, ...]) -> tuple[BuildProfile, ...]:
        return _unique_ids(value, "build profile")

    @model_validator(mode="after")
    def references_exist(self) -> Self:
        team_ids = {team.id for team in self.teams}
        referenced_team_ids = {self.ownership.primary_team_id, *self.ownership.supporting_team_ids}
        unknown_teams = referenced_team_ids - team_ids
        if unknown_teams:
            raise ValueError(
                f"ownership references unknown team ids: {', '.join(sorted(unknown_teams))}"
            )

        repository_ids = {repository.id for repository in self.integrations.git.repositories}
        unknown_repositories = {
            profile.repository_id
            for profile in self.build_profiles
            if profile.repository_id is not None and profile.repository_id not in repository_ids
        }
        if unknown_repositories:
            raise ValueError(
                "build_profiles reference unknown repository ids: "
                + ", ".join(sorted(unknown_repositories))
            )
        return self
