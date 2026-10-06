"""Canonical Atlas Travel fixture exercises existing platform contracts end to end."""

import re
from datetime import UTC, datetime
from pathlib import Path

from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.application.initiative_onboarding import InitiativeOnboardingService
from ai_dlc.application.initiative_validation import InitiativeValidationService
from ai_dlc.application.initiatives import (
    ConfigurationRevisionReason,
    InitiativeRegistry,
    InitiativeStatus,
    RegistryEventType,
)
from ai_dlc.domain.initiative import load_initiative_profile
from ai_dlc.domain.initiative.enums import KnowledgeSourceType, RepositoryAccess

SAMPLE = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "initiatives"
    / "samples"
    / "atlas-travel.yaml"
)
NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


def test_sample_loads_with_expected_identity_scope_and_repositories() -> None:
    profile = load_initiative_profile(SAMPLE)
    assert profile.schema_version == "1.0"
    assert profile.initiative.id == "atlas-travel"
    assert profile.initiative.name == "Atlas Travel"
    assert profile.ownership.primary_team_id == "atlas-engineering"
    assert profile.integrations.jira.enabled
    assert profile.integrations.jira.projects == ("ATLAS",)
    assert not profile.integrations.servicenow.enabled
    assert profile.integrations.git.enabled
    assert {repository.id for repository in profile.integrations.git.repositories} == {
        "atlas-travel-web",
        "atlas-travel-api",
    }
    assert all(
        repository.owner == "example-org"
        and repository.default_branch == "main"
        and repository.access == RepositoryAccess.READ_WRITE
        for repository in profile.integrations.git.repositories
    )


def test_sample_has_useful_knowledge_build_profiles_and_safe_policies() -> None:
    profile = load_initiative_profile(SAMPLE)
    assert {source.id for source in profile.knowledge.sources} == {
        "architecture",
        "product-requirements",
        "codebase",
    }
    assert {source.type for source in profile.knowledge.sources} == {
        KnowledgeSourceType.DOCUMENTATION,
        KnowledgeSourceType.REPOSITORY,
    }
    assert all(source.enabled and source.resource_id for source in profile.knowledge.sources)
    builds = {build.id: build for build in profile.build_profiles}
    assert set(builds) == {"api-python", "web-typescript"}
    assert builds["api-python"].repository_id == "atlas-travel-api"
    assert builds["api-python"].runtime == "3.12"
    assert builds["api-python"].test_command == "pytest"
    assert builds["web-typescript"].repository_id == "atlas-travel-web"
    assert builds["web-typescript"].build_command == "npm run build"
    assert builds["web-typescript"].test_command == "npm test"
    assert profile.policies.code_analysis.enabled
    assert profile.policies.code_generation.enabled
    assert profile.policies.git_write.enabled
    assert profile.policies.git_write.human_approval_required
    assert not profile.policies.jira_write.enabled
    assert not profile.policies.servicenow_write.enabled
    assert profile.defaults.model_roles.routing.value == "routing"
    assert profile.defaults.model_roles.reviewer.value == "independent_reviewer"


def test_sample_passes_readiness_and_onboards_as_revision_one() -> None:
    profile = load_initiative_profile(SAMPLE)
    validation = InitiativeValidationService(clock=lambda: NOW)
    report = validation.validate(profile)
    assert report.is_valid
    assert report.findings == ()

    events = InMemoryRegistryEventSink()
    registry = InitiativeRegistry(InMemoryInitiativeRepository(), events, clock=lambda: NOW)
    result = InitiativeOnboardingService(registry, validation).onboard_from_file(SAMPLE)
    assert result.succeeded
    assert result.initiative_id == "atlas-travel"
    assert result.validation_report.is_valid
    assert result.registration.status == InitiativeStatus.ACTIVE
    assert result.current_revision == 1
    assert registry.get(result.initiative_id).profile == profile
    revision = registry.get_revision(result.initiative_id, 1)
    assert revision.profile == profile
    assert revision.reason == ConfigurationRevisionReason.CREATED
    assert [event.event_type for event in events.events] == [RegistryEventType.INITIATIVE_CREATED]


def test_sample_contains_no_pos_or_credential_values() -> None:
    content = SAMPLE.read_text(encoding="utf-8")
    assert not re.search(r"(?i)\b(?:newpos|opos|pos|dt-pos|dt-central|discount tire)\b", content)
    assert not re.search(r"(?i)\b(?:password|token|secret|api[_-]?key|credential|pat)\b", content)
    assert "github.com/SeshasaiLDT" not in content
    assert "example-org" in content
