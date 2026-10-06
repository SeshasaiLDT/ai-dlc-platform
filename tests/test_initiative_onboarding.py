"""AIDLC-23 onboarding composes existing schema, readiness, and Registry contracts."""

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.application.initiative_onboarding import (
    InitiativeOnboardingResult,
    InitiativeOnboardingService,
    OnboardingStatus,
)
from ai_dlc.application.initiative_validation import (
    InitiativeValidationService,
    ValidationFinding,
    ValidationReport,
    ValidationSeverity,
)
from ai_dlc.application.initiatives import (
    ConfigurationRevision,
    InitiativeAlreadyExistsError,
    InitiativeNotFoundError,
    InitiativeRegistry,
    InitiativeStatus,
    RegisteredInitiative,
    RegistryEventType,
)
from ai_dlc.domain.initiative import (
    InitiativeProfile,
    ProfileValidationError,
    load_initiative_profile,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"
NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


@pytest.fixture
def travel() -> InitiativeProfile:
    return load_initiative_profile(EXAMPLES / "travel-platform.yaml")


@pytest.fixture
def repository() -> InMemoryInitiativeRepository:
    return InMemoryInitiativeRepository()


@pytest.fixture
def sink() -> InMemoryRegistryEventSink:
    return InMemoryRegistryEventSink()


@pytest.fixture
def registry(
    repository: InMemoryInitiativeRepository, sink: InMemoryRegistryEventSink
) -> InitiativeRegistry:
    return InitiativeRegistry(repository, sink, clock=lambda: NOW)


@pytest.fixture
def validation_service() -> InitiativeValidationService:
    return InitiativeValidationService(clock=lambda: NOW)


@pytest.fixture
def onboarding(
    registry: InitiativeRegistry, validation_service: InitiativeValidationService
) -> InitiativeOnboardingService:
    return InitiativeOnboardingService(registry, validation_service)


def revised(profile: InitiativeProfile, change: Callable[[dict], None]) -> InitiativeProfile:
    document = profile.model_dump(mode="json")
    change(document)
    return InitiativeProfile.model_validate(document)


class ExternalValidator:
    name = "external_resources"

    def __init__(self, severity: ValidationSeverity) -> None:
        self.severity = severity

    def validate(self, profile: InitiativeProfile) -> tuple[ValidationFinding, ...]:
        return (
            ValidationFinding(
                code="repository_access_unavailable",
                severity=self.severity,
                path="integrations.git.repositories[0]",
                message="Repository access could not be confirmed.",
                validator=self.name,
            ),
        )


def test_valid_profile_onboards_active_with_revision_one_and_report(
    onboarding: InitiativeOnboardingService,
    registry: InitiativeRegistry,
    sink: InMemoryRegistryEventSink,
    travel: InitiativeProfile,
) -> None:
    snapshot = travel.model_dump(mode="json")
    result = onboarding.onboard(travel)
    assert isinstance(result, InitiativeOnboardingResult)
    assert result.succeeded
    assert result.status == OnboardingStatus.SUCCEEDED
    assert result.initiative_id == travel.initiative.id
    assert isinstance(result.validation_report, ValidationReport)
    assert result.validation_report.is_valid
    assert result.validation_report.initiative_id == result.initiative_id
    assert result.registration.status == InitiativeStatus.ACTIVE
    assert result.registration.profile is travel
    assert result.current_revision == result.registration.current_revision == 1
    assert registry.get(result.initiative_id) is result.registration
    assert registry.get_revision(result.initiative_id, 1).profile is travel
    assert travel.model_dump(mode="json") == snapshot
    assert [event.event_type for event in sink.events] == [RegistryEventType.INITIATIVE_CREATED]


def test_readiness_warning_does_not_block_and_exact_report_is_preserved(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    class ReportSpy(InitiativeValidationService):
        def validate(self, profile: InitiativeProfile) -> ValidationReport:
            self.last_report = super().validate(profile)
            return self.last_report

    def remove_tests(document: dict) -> None:
        for build in document["build_profiles"]:
            build["test_command"] = None

    profile = revised(travel, remove_tests)
    validation = ReportSpy(clock=lambda: NOW)
    result = InitiativeOnboardingService(registry, validation).onboard(profile)
    assert result.succeeded
    assert result.validation_report is validation.last_report
    assert [warning.code for warning in result.validation_report.warnings] == [
        "code_generation_without_test_command"
    ]
    assert result.current_revision == 1


def test_readiness_errors_reject_without_record_revision_or_event(
    onboarding: InitiativeOnboardingService,
    registry: InitiativeRegistry,
    sink: InMemoryRegistryEventSink,
    travel: InitiativeProfile,
) -> None:
    def disable_git(document: dict) -> None:
        document["integrations"]["git"]["enabled"] = False

    profile = revised(travel, disable_git)
    result = onboarding.onboard(profile)
    assert result.status == OnboardingStatus.REJECTED
    assert not result.succeeded
    assert result.initiative_id == travel.initiative.id
    assert result.registration is None
    assert result.current_revision is None
    assert not result.validation_report.is_valid
    assert result.validation_report.errors[0].code == "write_policy_requires_integration"
    assert registry.list() == ()
    with pytest.raises(InitiativeNotFoundError):
        registry.get_revision(result.initiative_id, 1)
    assert sink.events == ()


def test_rejection_leaves_existing_registry_state_unchanged(
    onboarding: InitiativeOnboardingService,
    registry: InitiativeRegistry,
    sink: InMemoryRegistryEventSink,
    travel: InitiativeProfile,
) -> None:
    first = onboarding.onboard(travel)

    def disable_jira(document: dict) -> None:
        document["integrations"]["jira"]["enabled"] = False
        document["policies"]["jira_write"]["enabled"] = True

    result = onboarding.onboard(revised(travel, disable_jira))
    assert result.status == OnboardingStatus.REJECTED
    assert registry.get(travel.initiative.id) is first.registration
    assert len(registry.list_revisions(travel.initiative.id)) == 1
    assert len(sink.events) == 1


def test_file_based_onboarding_uses_profile_loader(
    onboarding: InitiativeOnboardingService, registry: InitiativeRegistry
) -> None:
    result = onboarding.onboard_from_file(EXAMPLES / "field-operations.yaml")
    assert result.succeeded
    assert result.initiative_id == "field-operations"
    assert result.current_revision == 1
    assert registry.get_revision(result.initiative_id, 1).profile == result.registration.profile


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("initiative: [unterminated", "line"),
        ("schema_version: '1.0'\ninitiative: {}\n", "initiative.id"),
    ],
)
def test_invalid_file_raises_typed_profile_error_without_registration(
    onboarding: InitiativeOnboardingService,
    registry: InitiativeRegistry,
    sink: InMemoryRegistryEventSink,
    tmp_path: Path,
    contents: str,
    message: str,
) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ProfileValidationError, match=message):
        onboarding.onboard_from_file(path)
    assert registry.list() == ()
    assert sink.events == ()


def test_missing_file_raises_typed_profile_error(
    onboarding: InitiativeOnboardingService, registry: InitiativeRegistry, tmp_path: Path
) -> None:
    with pytest.raises(ProfileValidationError, match="cannot read profile"):
        onboarding.onboard_from_file(tmp_path / "missing.yaml")
    assert registry.list() == ()


def test_duplicate_id_surfaces_registry_conflict_without_new_revision_or_event(
    onboarding: InitiativeOnboardingService,
    registry: InitiativeRegistry,
    sink: InMemoryRegistryEventSink,
    travel: InitiativeProfile,
) -> None:
    onboarding.onboard(travel)
    with pytest.raises(InitiativeAlreadyExistsError, match=travel.initiative.id):
        onboarding.onboard(travel)
    assert [item.revision for item in registry.list_revisions(travel.initiative.id)] == [1]
    assert len(sink.events) == 1


def test_both_example_profiles_onboard_without_workflow_changes(
    onboarding: InitiativeOnboardingService, registry: InitiativeRegistry
) -> None:
    results = tuple(
        onboarding.onboard_from_file(EXAMPLES / filename)
        for filename in ("travel-platform.yaml", "field-operations.yaml")
    )
    assert all(result.succeeded and result.current_revision == 1 for result in results)
    assert {result.initiative_id for result in results} == {
        "travel-platform",
        "field-operations",
    }
    assert len(registry.list()) == 2


@pytest.mark.parametrize("severity", [ValidationSeverity.ERROR, ValidationSeverity.WARNING])
def test_injected_external_findings_control_onboarding(
    registry: InitiativeRegistry,
    travel: InitiativeProfile,
    severity: ValidationSeverity,
) -> None:
    validation = InitiativeValidationService((ExternalValidator(severity),), clock=lambda: NOW)
    result = InitiativeOnboardingService(registry, validation).onboard(travel)
    assert result.validation_report.findings[0].code == "repository_access_unavailable"
    assert result.validation_report.findings[0].path == "integrations.git.repositories[0]"
    assert result.succeeded is (severity == ValidationSeverity.WARNING)
    assert len(registry.list()) == (1 if result.succeeded else 0)


def test_result_is_immutable(
    onboarding: InitiativeOnboardingService, travel: InitiativeProfile
) -> None:
    result = onboarding.onboard(travel)
    with pytest.raises(FrozenInstanceError):
        result.status = OnboardingStatus.REJECTED
    with pytest.raises(FrozenInstanceError):
        result.registration = None


def test_validation_runs_before_registry_create(travel: InitiativeProfile) -> None:
    calls: list[str] = []

    class ValidationSpy(InitiativeValidationService):
        def validate(self, profile: InitiativeProfile) -> ValidationReport:
            calls.append("validate")
            return super().validate(profile)

    class RepositorySpy(InMemoryInitiativeRepository):
        def insert_with_revision(
            self, initiative: RegisteredInitiative, revision: ConfigurationRevision
        ) -> None:
            calls.append("create")
            assert calls == ["validate", "create"]
            super().insert_with_revision(initiative, revision)

    registry = InitiativeRegistry(RepositorySpy(), InMemoryRegistryEventSink(), clock=lambda: NOW)
    result = InitiativeOnboardingService(registry, ValidationSpy(clock=lambda: NOW)).onboard(travel)
    assert result.succeeded
    assert calls == ["validate", "create"]


def test_validator_crash_is_rejection_not_registration(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    class CrashingValidator:
        name = "external"

        def validate(self, profile: InitiativeProfile) -> tuple[ValidationFinding, ...]:
            raise RuntimeError("private diagnostic")

    validation = InitiativeValidationService((CrashingValidator(),), clock=lambda: NOW)
    result = InitiativeOnboardingService(registry, validation).onboard(travel)
    assert not result.succeeded
    assert result.validation_report.errors[0].code == "validator_execution_failed"
    assert registry.list() == ()


def test_unexpected_validation_service_failure_is_not_a_readiness_rejection(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    class BrokenService(InitiativeValidationService):
        def validate(self, profile: InitiativeProfile) -> ValidationReport:
            raise RuntimeError("validation service unavailable")

    onboarding = InitiativeOnboardingService(registry, BrokenService())
    with pytest.raises(RuntimeError, match="validation service unavailable"):
        onboarding.onboard(travel)
    assert registry.list() == ()
