"""Load, validate, and export the Initiative Profile contract."""

import argparse
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import InitiativeProfile


class ProfileValidationError(ValueError):
    """A profile cannot be read or does not satisfy the domain contract."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Prevent YAML key shadowing before Pydantic sees the document."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate YAML key '{key}'", key_node.start_mark
            )
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _format_location(location: tuple[str | int, ...]) -> str:
    result = ""
    for part in location:
        if isinstance(part, int):
            result += f"[{part}]"
        elif result:
            result += f".{part}"
        else:
            result = part
    return result or "profile"


def _format_validation_error(error: ValidationError) -> str:
    messages = []
    for issue in error.errors(include_url=False, include_input=False):
        message = issue["msg"].removeprefix("Value error, ")
        if issue["type"] == "missing":
            message = "field is required"
        messages.append(f"{_format_location(issue['loc'])}: {message}")
    return "\n".join(messages)


def load_initiative_profile(path: str | Path) -> InitiativeProfile:
    """Read one YAML profile and return a validated, typed domain model."""
    profile_path = Path(path)
    try:
        with profile_path.open(encoding="utf-8") as source:
            document = yaml.load(source, Loader=_UniqueKeyLoader)
    except OSError as exc:
        raise ProfileValidationError(
            f"{profile_path}: cannot read profile: {exc.strerror}"
        ) from exc
    except yaml.YAMLError as exc:
        location = getattr(exc, "problem_mark", None)
        suffix = f" at line {location.line + 1}, column {location.column + 1}" if location else ""
        problem = getattr(exc, "problem", None) or "malformed YAML"
        raise ProfileValidationError(f"{profile_path}: {problem}{suffix}") from exc

    if not isinstance(document, dict):
        raise ProfileValidationError(f"{profile_path}: profile: expected a YAML mapping")
    try:
        return InitiativeProfile.model_validate(document)
    except ValidationError as exc:
        raise ProfileValidationError(f"{profile_path}:\n{_format_validation_error(exc)}") from exc


def export_json_schema(path: str | Path) -> None:
    """Write a stable JSON Schema generated from the canonical Pydantic model."""
    schema = InitiativeProfile.model_json_schema()
    Path(path).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an AI-DLC Initiative Profile")
    parser.add_argument("profile", type=Path, help="YAML profile to validate")
    args = parser.parse_args()
    try:
        profile = load_initiative_profile(args.profile)
    except ProfileValidationError as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Valid Initiative Profile: {profile.initiative.id} (schema {profile.schema_version})")
    return 0
