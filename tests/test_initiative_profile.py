"""AIDLC-19 contract tests for human-authored initiative profiles."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from ai_dlc.domain.initiative import (
    InitiativeProfile,
    ProfileValidationError,
    export_json_schema,
    load_initiative_profile,
)
from ai_dlc.domain.initiative.enums import GitProvider, ModelRole, RepositoryAccess

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"


@pytest.fixture
def travel_document() -> dict:
    return yaml.safe_load((EXAMPLES / "travel-platform.yaml").read_text(encoding="utf-8"))


def validate_document(document: dict, tmp_path: Path) -> InitiativeProfile:
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_initiative_profile(path)


def assert_invalid(document: dict, tmp_path: Path, expected: str) -> None:
    with pytest.raises(ProfileValidationError, match=expected):
        validate_document(document, tmp_path)


@pytest.mark.parametrize("filename", ["travel-platform.yaml", "field-operations.yaml"])
def test_examples_load_and_are_distinct(filename: str) -> None:
    profile = load_initiative_profile(EXAMPLES / filename)
    assert profile.schema_version == "1.0"
    assert profile.ownership.primary_team_id in {team.id for team in profile.teams}


def test_travel_scope_build_profiles_and_policies() -> None:
    profile = load_initiative_profile(EXAMPLES / "travel-platform.yaml")
    assert profile.initiative.id == "travel-platform"
    assert profile.integrations.git.provider == GitProvider.GITHUB
    assert len(profile.integrations.git.repositories) == 2
    assert profile.integrations.git.repositories[0].access == RepositoryAccess.READ_WRITE
    assert profile.integrations.jira.projects == ("TRAVEL", "JOURNEY")
    assert not profile.integrations.servicenow.enabled
    assert len(profile.build_profiles) == 2
    assert profile.build_profiles[0].test_command == "pytest"
    assert profile.policies.git_write.enabled
    assert profile.policies.git_write.human_approval_required
    assert profile.defaults.model_roles.reviewer == ModelRole.INDEPENDENT_REVIEWER


def test_operational_profile_has_different_scope() -> None:
    profile = load_initiative_profile(EXAMPLES / "field-operations.yaml")
    assert not profile.integrations.jira.enabled
    assert profile.integrations.servicenow.enabled
    assert profile.integrations.servicenow.scopes == ("incident", "change")
    assert profile.integrations.git.provider == GitProvider.GITLAB
    assert not profile.policies.git_write.enabled
    assert profile.policies.servicenow_write.human_approval_required


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("integrations", "git", "repositories"), "duplicate repository id"),
        (("knowledge", "sources"), "duplicate knowledge source id"),
        (("build_profiles",), "duplicate build profile id"),
        (("teams",), "duplicate team id"),
    ],
)
def test_duplicate_ids_rejected(
    travel_document: dict, tmp_path: Path, path: tuple, expected: str
) -> None:
    target = travel_document
    for key in path:
        target = target[key]
    target.append(copy.deepcopy(target[0]))
    assert_invalid(travel_document, tmp_path, expected)


def test_enabled_jira_requires_projects(travel_document: dict, tmp_path: Path) -> None:
    travel_document["integrations"]["jira"]["projects"] = []
    assert_invalid(travel_document, tmp_path, "integrations.jira: enabled Jira integration")


def test_disabled_integrations_need_no_scope(travel_document: dict, tmp_path: Path) -> None:
    travel_document["integrations"] = {
        "jira": {"enabled": False},
        "servicenow": {"enabled": False},
        "git": {"enabled": False},
    }
    travel_document["build_profiles"] = []
    profile = validate_document(travel_document, tmp_path)
    assert not profile.integrations.jira.projects
    assert not profile.integrations.git.repositories


def test_invalid_access_rejected_at_field(travel_document: dict, tmp_path: Path) -> None:
    travel_document["integrations"]["git"]["repositories"][0]["access"] = "admin"
    assert_invalid(travel_document, tmp_path, r"integrations.git.repositories\[0\].access")


def test_unsupported_version_rejected(travel_document: dict, tmp_path: Path) -> None:
    travel_document["schema_version"] = "2.0"
    assert_invalid(travel_document, tmp_path, "schema_version: unsupported schema version '2.0'")


@pytest.mark.parametrize("missing", ["initiative", "id", "name"])
def test_missing_identity_rejected(travel_document: dict, tmp_path: Path, missing: str) -> None:
    if missing == "initiative":
        del travel_document["initiative"]
        expected = "initiative: field is required"
    else:
        del travel_document["initiative"][missing]
        expected = f"initiative.{missing}: field is required"
    assert_invalid(travel_document, tmp_path, expected)


def test_blank_identity_rejected(travel_document: dict, tmp_path: Path) -> None:
    travel_document["initiative"]["id"] = ""
    assert_invalid(travel_document, tmp_path, "initiative.id")


def test_missing_nested_field_has_actionable_location(
    travel_document: dict, tmp_path: Path
) -> None:
    del travel_document["integrations"]["git"]["repositories"][0]["default_branch"]
    assert_invalid(
        travel_document,
        tmp_path,
        r"integrations.git.repositories\[0\].default_branch: field is required",
    )


def test_malformed_yaml_and_duplicate_keys_are_clean_errors(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("initiative: [\n", encoding="utf-8")
    with pytest.raises(ProfileValidationError, match="line 2, column 1"):
        load_initiative_profile(path)
    path.write_text('schema_version: "1.0"\nschema_version: "2.0"\n', encoding="utf-8")
    with pytest.raises(ProfileValidationError, match="duplicate YAML key 'schema_version'"):
        load_initiative_profile(path)


def test_unknown_secret_field_rejected(travel_document: dict, tmp_path: Path) -> None:
    travel_document["integrations"]["jira"]["api_token"] = "placeholder"
    assert_invalid(travel_document, tmp_path, "integrations.jira.api_token")


def test_broken_references_and_absolute_build_path_rejected(
    travel_document: dict, tmp_path: Path
) -> None:
    travel_document["ownership"]["primary_team_id"] = "unknown-team"
    assert_invalid(travel_document, tmp_path, "ownership references unknown team ids")
    travel_document["ownership"]["primary_team_id"] = "travel-engineering"
    travel_document["build_profiles"][0]["repository_id"] = "unknown-repo"
    assert_invalid(travel_document, tmp_path, "build_profiles reference unknown repository ids")
    travel_document["build_profiles"][0]["repository_id"] = "travel-api"
    travel_document["build_profiles"][0]["working_directory"] = "/absolute/path"
    assert_invalid(travel_document, tmp_path, "must be a relative repository path")


def test_json_schema_export_is_deterministic_and_rejects_unknown_fields(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    export_json_schema(first)
    export_json_schema(second)
    assert first.read_bytes() == second.read_bytes()
    schema = json.loads(first.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["JiraIntegration"]["additionalProperties"] is False
    assert "schema_version" in schema["required"]
    assert schema["properties"]["schema_version"]["const"] == "1.0"
