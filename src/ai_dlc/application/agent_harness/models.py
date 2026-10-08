"""Small, JSON-safe values shared by independently deployed agents."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, field_validator, model_validator

from ai_dlc.application.approval import ApprovalStatus
from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.domain.identity import Principal

INTERFACE_VERSION = "1.4.0"
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])

type FrozenJsonValue = (
    str | int | float | bool | None | tuple[FrozenJsonValue, ...] | Mapping[str, FrozenJsonValue]
)


def _freeze_json(value: JsonValue) -> FrozenJsonValue:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _json_object(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Reject non-JSON values and non-finite numbers at every public payload boundary."""
    json.dumps(value, allow_nan=False)
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ExecutionError(ContractModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, JsonValue] | None = None

    @field_validator("code", "message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("error code and message must be nonblank")
        return value

    @field_validator("details")
    @classmethod
    def json_details(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        return _json_object(value) if value is not None else None


class Invocation(ContractModel):
    input: dict[str, JsonValue]
    metadata: dict[str, JsonValue] | None = None

    @field_validator("input", "metadata")
    @classmethod
    def json_fields(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        return _json_object(value) if value is not None else None


class ExecutionResult(ContractModel):
    status: ExecutionStatus
    correlation_id: str
    trace_id: str
    request_id: str | None = None
    output: dict[str, JsonValue] | None = None
    error: ExecutionError | None = None
    metadata: dict[str, JsonValue] | None = None

    @field_validator("correlation_id", "trace_id", "request_id")
    @classmethod
    def nonblank_id(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("result identifiers must be nonblank")
        return value

    @field_validator("output", "metadata")
    @classmethod
    def json_fields(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        return _json_object(value) if value is not None else None

    @model_validator(mode="after")
    def consistent_status(self) -> ExecutionResult:
        if self.status is ExecutionStatus.SUCCEEDED and self.error is not None:
            raise ValueError("successful result cannot contain an error")
        if self.status is not ExecutionStatus.SUCCEEDED and self.error is None:
            raise ValueError("unsuccessful result requires a structured error")
        if self.status is not ExecutionStatus.SUCCEEDED and self.output is not None:
            raise ValueError("unsuccessful result cannot contain output")
        return self


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Trusted in-process state, constructed after authentication and authorization.

    Never accept this object or its authorization snapshot from an agent payload.
    """

    request_id: str
    correlation_id: str
    session_id: str
    trace_id: str
    authorization: ResolvedAuthorizationContext
    deadline: datetime | None = None
    cancelled: bool = False
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    task_id: str | None = None
    cancellation_event: asyncio.Event | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("request_id", "correlation_id", "session_id", "trace_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonblank")
        if not isinstance(self.authorization, ResolvedAuthorizationContext):
            raise TypeError("trusted resolved authorization context required")
        if self.deadline is not None and (
            not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise ValueError("deadline must be timezone-aware")
        if type(self.cancelled) is not bool:
            raise TypeError("cancelled must be a bool")
        if self.task_id is not None and (
            not isinstance(self.task_id, str) or not self.task_id.strip()
        ):
            raise ValueError("task_id must be nonblank")
        if self.cancellation_event is not None and not isinstance(
            self.cancellation_event, asyncio.Event
        ):
            raise TypeError("cancellation_event must be an asyncio.Event")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a JSON object")
        metadata = _JSON_OBJECT.validate_python(dict(self.metadata), strict=True)
        object.__setattr__(
            self,
            "metadata",
            _freeze_json(json.loads(json.dumps(metadata, allow_nan=False))),
        )

    @property
    def principal(self) -> Principal:
        return self.authorization.principal

    @property
    def initiative_id(self) -> str:
        return self.authorization.initiative_id

    @property
    def cancellation_requested(self) -> bool:
        return self.cancelled or (
            self.cancellation_event is not None and self.cancellation_event.is_set()
        )


class ApprovalIntent(ContractModel):
    """A request to the trusted approval boundary, not an authorization decision."""

    request_key: str
    operation: str
    logical_target: dict[str, JsonValue]

    @field_validator("request_key", "operation")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approval identifiers must be nonblank")
        return value

    @field_validator("logical_target")
    @classmethod
    def json_target(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _json_object(value)


class ApprovalReference(ContractModel):
    approval_id: str
    status: ApprovalStatus

    @field_validator("approval_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approval reference fields must be nonblank")
        return value


class ValidationResult(ContractModel):
    valid: bool
    errors: tuple[ExecutionError, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> ValidationResult:
        if self.valid == bool(self.errors):
            raise ValueError("valid must be true exactly when errors are empty")
        return self
