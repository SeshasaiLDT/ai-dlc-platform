"""Versioned, secret-free agent capability manifests."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import JsonValue, field_validator, model_validator

from ai_dlc.application.agent_harness.models import ContractModel

_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class AgentHealth(ContractModel):
    status: Literal["unknown", "healthy", "degraded", "unhealthy"] = "unknown"
    checked_at: datetime | None = None
    message: str | None = None


class AgentManifest(ContractModel):
    """Discovery metadata. Permission labels describe requested access; they grant none."""

    manifest_version: str = "1.0.0"
    agent_id: str
    name: str
    description: str
    agent_version: str
    a2a_endpoint: str
    capabilities: tuple[str, ...]
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    permissions: frozenset[str] = frozenset()
    health: AgentHealth = AgentHealth()

    @field_validator("manifest_version", "agent_version")
    @classmethod
    def semver(cls, value: str) -> str:
        if not _VERSION.fullmatch(value):
            raise ValueError("versions must use MAJOR.MINOR.PATCH")
        return value

    @field_validator("agent_id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("agent_id must be lowercase kebab-case")
        return value

    @field_validator("name", "description")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name and description must not be blank")
        return value

    @field_validator("a2a_endpoint")
    @classmethod
    def endpoint(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("a2a_endpoint must be an HTTP(S) URL")
        return value

    @field_validator("capabilities")
    @classmethod
    def valid_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item.strip() for item in value) or len(set(value)) != len(value):
            raise ValueError("capabilities must be nonempty, unique names")
        return value

    @model_validator(mode="after")
    def valid_schemas(self) -> AgentManifest:
        for schema in (self.input_schema, self.output_schema):
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as error:
                raise ValueError(
                    "input and output must be valid Draft 2020-12 JSON Schemas"
                ) from error
            if _has_external_ref(schema):
                raise ValueError("schemas must not contain external references")
        return self


class RegisteredAgent(ContractModel):
    manifest: AgentManifest
    revision: int
    updated_at: datetime
    enabled: bool = True


def _has_external_ref(value: JsonValue) -> bool:
    if isinstance(value, dict):
        return any(
            (key == "$ref" and isinstance(item, str) and not item.startswith("#"))
            or _has_external_ref(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_external_ref(item) for item in value)
    return False
