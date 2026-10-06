"""AIDLC-21 readiness validation contracts, without live integration calls."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_dlc.application.initiative_validation import (
    InitiativeValidationService,
    ValidationFinding,
    ValidationReport,
    ValidationSeverity,
)
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"
NOW = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


@pytest.fixture
def travel() -> InitiativeProfile:
    return load_initiative_profile(EXAMPLES / "travel-platform.yaml")


def altered(profile: InitiativeProfile, edit: object) -> InitiativeProfile:
    document = profile.model_dump(mode="json")
    edit(document)
    return InitiativeProfile.model_validate(document)


class FakeValidator:
    def __init__(self, name: str, findings: tuple[ValidationFinding, ...]) -> None:
        self.name = name
        self.findings = findings

    def validate(self, profile: InitiativeProfile) -> tuple[ValidationFinding, ...]:
        return self.findings


class CrashingValidator:
    name = "external_jira"

    def validate(self, profile: InitiativeProfile) -> tuple[ValidationFinding, ...]:
        raise RuntimeError("private upstream diagnostic")


def finding(
    code: str, severity: ValidationSeverity, path: str, validator: str = "external"
) -> ValidationFinding:
    return ValidationFinding(code, severity, path, f"Check {path}.", validator)


def test_valid_examples_are_independently_ready_with_utc_report(travel: InitiativeProfile) -> None:
    service = InitiativeValidationService(clock=lambda: NOW)
    for filename in ("travel-platform.yaml", "field-operations.yaml"):
        profile = load_initiative_profile(EXAMPLES / filename)
        report = service.validate(profile)
        assert isinstance(report, ValidationReport)
        assert report.initiative_id == profile.initiative.id
        assert report.validated_at == NOW
        assert report.validated_at.tzinfo == UTC
        assert report.is_valid
        assert report.findings == ()
    assert service.validate(travel).initiative_id == "travel-platform"


@pytest.mark.parametrize(
    ("integration", "policy"),
    [("jira", "jira_write"), ("servicenow", "servicenow_write"), ("git", "git_write")],
)
def test_enabled_write_policy_requires_integration(
    travel: InitiativeProfile, integration: str, policy: str
) -> None:
    def edit(document: dict) -> None:
        document["integrations"][integration]["enabled"] = False
        document["policies"][policy]["enabled"] = True

    report = InitiativeValidationService().validate(altered(travel, edit))
    assert not report.is_valid
    assert any(
        item.code == "write_policy_requires_integration"
        and item.path == f"policies.{policy}.enabled"
        and item.severity == ValidationSeverity.ERROR
        for item in report.errors
    )


def test_approval_on_disabled_write_policy_is_warning(travel: InitiativeProfile) -> None:
    def edit(document: dict) -> None:
        document["policies"]["jira_write"] = {
            "enabled": False,
            "human_approval_required": True,
        }

    report = InitiativeValidationService().validate(altered(travel, edit))
    assert report.is_valid
    assert report.errors == ()
    assert [(item.code, item.path) for item in report.warnings] == [
        ("approval_on_disabled_write_policy", "policies.jira_write.human_approval_required")
    ]


def test_read_write_repository_with_disabled_git_writes_warns(travel: InitiativeProfile) -> None:
    def edit(document: dict) -> None:
        document["policies"]["git_write"]["enabled"] = False

    report = InitiativeValidationService().validate(altered(travel, edit))
    assert report.is_valid
    assert report.warnings[0].code == "repository_write_access_unused"
    assert report.warnings[0].path == "integrations.git.repositories[0].access"


@pytest.mark.parametrize(
    ("build_profiles", "expected_code"),
    [
        ([], "code_generation_without_build_profiles"),
        (None, "code_generation_without_test_command"),
    ],
)
def test_code_generation_without_verification_configuration_warns(
    travel: InitiativeProfile, build_profiles: list | None, expected_code: str
) -> None:
    def edit(document: dict) -> None:
        if build_profiles is None:
            for build in document["build_profiles"]:
                build["test_command"] = None
        else:
            document["build_profiles"] = build_profiles

    report = InitiativeValidationService().validate(altered(travel, edit))
    assert report.is_valid
    assert report.warnings[0].code == expected_code
    assert report.warnings[0].path == "build_profiles"


def test_code_generation_does_not_require_git_writes(travel: InitiativeProfile) -> None:
    def edit(document: dict) -> None:
        document["policies"]["git_write"]["enabled"] = False
        document["policies"]["git_write"]["human_approval_required"] = False
        for repository in document["integrations"]["git"]["repositories"]:
            repository["access"] = "read_only"

    assert InitiativeValidationService().validate(altered(travel, edit)).findings == ()


def test_external_validator_can_report_missing_resources_without_activation(
    travel: InitiativeProfile,
) -> None:
    validators = (
        FakeValidator(
            "jira_access",
            (
                finding(
                    "jira_project_inaccessible",
                    ValidationSeverity.ERROR,
                    "integrations.jira.projects[0]",
                ),
            ),
        ),
        FakeValidator(
            "git_access",
            (
                finding(
                    "git_repository_missing",
                    ValidationSeverity.ERROR,
                    "integrations.git.repositories[0]",
                ),
                finding(
                    "git_access_unknown",
                    ValidationSeverity.WARNING,
                    "integrations.git.repositories[0].access",
                ),
            ),
        ),
        FakeValidator(
            "knowledge_access",
            (
                finding(
                    "knowledge_source_missing", ValidationSeverity.ERROR, "knowledge.sources[0]"
                ),
            ),
        ),
        FakeValidator(
            "servicenow_access",
            (
                finding(
                    "servicenow_scope_missing",
                    ValidationSeverity.ERROR,
                    "integrations.servicenow.scopes[0]",
                ),
            ),
        ),
    )
    report = InitiativeValidationService(validators, clock=lambda: NOW).validate(travel)
    assert not report.is_valid
    assert len(report.errors) == 4
    assert len(report.warnings) == 1
    assert {item.code for item in report.findings} == {
        "jira_project_inaccessible",
        "git_repository_missing",
        "git_access_unknown",
        "knowledge_source_missing",
        "servicenow_scope_missing",
    }
    assert all(item.path and item.validator for item in report.findings)


def test_validator_crash_becomes_structured_error_without_leaking_diagnostic(
    travel: InitiativeProfile,
) -> None:
    report = InitiativeValidationService((CrashingValidator(),)).validate(travel)
    assert not report.is_valid
    assert report.errors[0].code == "validator_execution_failed"
    assert report.errors[0].validator == "external_jira"
    assert "private upstream diagnostic" not in report.errors[0].message


def test_invalid_validator_return_becomes_structured_error(travel: InitiativeProfile) -> None:
    class BrokenValidator:
        name = "broken"

        def validate(self, profile: InitiativeProfile) -> list:
            return []

    report = InitiativeValidationService((BrokenValidator(),)).validate(travel)
    assert [item.code for item in report.errors] == ["validator_execution_failed"]


def test_finding_order_is_independent_of_validator_registration_order(
    travel: InitiativeProfile,
) -> None:
    first = FakeValidator(
        "first",
        (
            finding("z", ValidationSeverity.INFO, "z"),
            finding("b", ValidationSeverity.ERROR, "a"),
        ),
    )
    second = FakeValidator(
        "second",
        (
            finding("a", ValidationSeverity.ERROR, "a"),
            finding("a", ValidationSeverity.WARNING, "b"),
        ),
    )
    forward = InitiativeValidationService((first, second), clock=lambda: NOW).validate(travel)
    reverse = InitiativeValidationService((second, first), clock=lambda: NOW).validate(travel)
    assert forward == reverse
    assert [(item.severity, item.path, item.code) for item in forward.findings] == [
        (ValidationSeverity.ERROR, "a", "a"),
        (ValidationSeverity.ERROR, "a", "b"),
        (ValidationSeverity.WARNING, "b", "a"),
        (ValidationSeverity.INFO, "z", "z"),
    ]
    assert len(forward.info) == 1


def test_validation_preserves_profile_and_result_is_immutable(travel: InitiativeProfile) -> None:
    snapshot = travel.model_dump(mode="json")
    report = InitiativeValidationService(clock=lambda: NOW).validate(travel)
    assert travel.model_dump(mode="json") == snapshot
    with pytest.raises(FrozenInstanceError):
        report.initiative_id = "changed"
    with pytest.raises(FrozenInstanceError):
        finding("code", ValidationSeverity.INFO, "path").code = "changed"


def test_clock_normalizes_offset_to_utc_and_rejects_naive_time(travel: InitiativeProfile) -> None:
    offset = timezone(timedelta(hours=-7))
    report = InitiativeValidationService(clock=lambda: NOW.astimezone(offset)).validate(travel)
    assert report.validated_at == NOW
    assert report.validated_at.tzinfo == UTC
    with pytest.raises(ValueError, match="timezone-aware"):
        InitiativeValidationService(clock=lambda: datetime(2026, 1, 2)).validate(travel)


def test_requires_validated_profile() -> None:
    with pytest.raises(TypeError, match="validated InitiativeProfile"):
        InitiativeValidationService().validate({"initiative": {"id": "example"}})
