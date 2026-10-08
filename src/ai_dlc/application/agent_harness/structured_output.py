"""Strict structured-output validation over existing harness result contracts."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from time import monotonic

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, JsonValue, ValidationError, field_validator

from .mcp import _local_references_only
from .models import (
    AgentContext,
    ContractModel,
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    ValidationResult,
)
from .ports import TelemetryProvider

_MESSAGES = {
    "malformed_json": "Invalid JSON output",
    "missing_required_field": "Required output field missing",
    "invalid_field_type": "Invalid output field type",
    "invalid_enum_value": "Unsupported output value",
    "invalid_nested_object": "Invalid nested output object",
    "unexpected_property": "Unexpected output property",
    "schema_violation": "Output does not match schema",
    "artifact_schema_failure": "Invalid artifact reference",
    "repair_exhausted": "Output repair attempts exhausted",
    "validation_cancelled": "Output validation cancelled",
    "validation_timed_out": "Output validation deadline exceeded",
}

type RepairCallback = Callable[[str, ValidationResult, AgentContext], Awaitable[str]]


class ArtifactReference(ContractModel):
    """Opaque logical reference; access control and storage remain external."""

    artifact_id: str
    store_id: str
    metadata: dict[str, JsonValue] | None = None

    @field_validator("artifact_id", "store_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact identifiers must be nonblank")
        return value

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if value is not None and len(json.dumps(value, allow_nan=False)) > 16_384:
            raise ValueError("artifact metadata too large")
        return value


def _safe_schema(schema: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        copy = json.loads(json.dumps(dict(schema), allow_nan=False))
        if not _local_references_only(copy):
            raise ValueError("external schema references are unsupported")
        Draft202012Validator.check_schema(copy)
    except (TypeError, ValueError, SchemaError) as exc:
        raise ValueError("invalid local JSON Schema") from exc
    return copy


def _error(code: str, schema_id: str, path: Sequence[str | int] = ()) -> ExecutionError:
    details: dict[str, JsonValue] = {"schema_id": schema_id, "path_depth": len(path)}
    if code == "artifact_schema_failure" and path:
        details["artifact_index"] = path[0]
    return ExecutionError(
        code=code,
        message=_MESSAGES[code],
        details=details,
    )


def _pydantic_code(kind: str, depth: int) -> str:
    if kind == "missing":
        return "missing_required_field"
    if kind == "extra_forbidden":
        return "unexpected_property"
    if kind in ("enum", "literal_error"):
        return "invalid_enum_value"
    if kind in ("model_type", "dict_type") and depth > 0:
        return "invalid_nested_object"
    if kind.endswith(("_type", "_parsing")) or kind in ("model_type", "dict_type"):
        return "invalid_field_type"
    return "schema_violation"


def _schema_code(kind: str, expected: object, depth: int) -> str:
    return {
        "required": "missing_required_field",
        "type": "invalid_nested_object"
        if depth > 0 and expected == "object"
        else "invalid_field_type",
        "enum": "invalid_enum_value",
        "const": "invalid_enum_value",
        "additionalProperties": "unexpected_property",
    }.get(kind, "schema_violation")


class StructuredOutputValidator:
    """One immutable schema configuration; all invocation state remains local."""

    def __init__(
        self,
        schema_id: str,
        *,
        model: type[BaseModel] | None = None,
        json_schema: Mapping[str, JsonValue] | None = None,
        artifact_metadata_schema: Mapping[str, JsonValue] | None = None,
        max_repair_attempts: int = 0,
        telemetry: TelemetryProvider | None = None,
    ) -> None:
        if (
            not isinstance(schema_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", schema_id) is None
        ):
            raise ValueError("schema_id must be a stable nonblank identifier")
        if (model is None) == (json_schema is None):
            raise ValueError("choose exactly one Pydantic model or JSON Schema")
        if model is not None and (not isinstance(model, type) or not issubclass(model, BaseModel)):
            raise TypeError("model must be a Pydantic BaseModel subclass")
        if type(max_repair_attempts) is not int or not 0 <= max_repair_attempts <= 3:
            raise ValueError("max_repair_attempts must be between 0 and 3")
        self.schema_id = schema_id
        self._model = model
        self._schema = (
            Draft202012Validator(
                _safe_schema(json_schema), format_checker=Draft202012Validator.FORMAT_CHECKER
            )
            if json_schema is not None
            else None
        )
        self._artifact_schema = (
            Draft202012Validator(
                _safe_schema(artifact_metadata_schema),
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            )
            if artifact_metadata_schema is not None
            else None
        )
        self._max_repair_attempts = max_repair_attempts
        self._telemetry = telemetry

    def validate(self, request: Invocation, *, context: AgentContext) -> ValidationResult:
        """Existing Validator port for already-parsed JSON object input."""
        if not isinstance(request, Invocation) or not isinstance(context, AgentContext):
            raise TypeError("Invocation and trusted AgentContext required")
        started = monotonic()
        stopped = self._stopped(context)
        if stopped is not None:
            validation = ValidationResult(valid=False, errors=(_error(stopped, self.schema_id),))
        else:
            _, validation = self._validate_data(request.input)
        event = "validation.success" if validation.valid else "validation.failure"
        self._emit("record_event", f"{event}.{self.schema_id}", context=context)
        for error in validation.errors:
            self._emit("record_error", error, context=context)
        self._emit(
            "record_metric",
            "validation.duration_ms",
            (monotonic() - started) * 1000,
            context=context,
        )
        return validation

    async def parse(
        self,
        raw_json: str,
        *,
        context: AgentContext,
        artifacts: Sequence[ArtifactReference | Mapping[str, JsonValue]] = (),
        repair: RepairCallback | None = None,
    ) -> ExecutionResult:
        """Parse, validate and optionally regenerate; never accept invalid repair output."""
        if not isinstance(raw_json, str) or not isinstance(context, AgentContext):
            raise TypeError("JSON text and trusted AgentContext required")
        started = monotonic()
        attempts = 0
        current = raw_json
        last: ValidationResult | None = None
        outcome: ExecutionResult
        try:
            while True:
                terminal = self._stopped(context)
                if terminal is not None:
                    outcome = self._result(context, terminal, attempts)
                    break
                data, validation = self._parse_once(current)
                last = validation
                if validation.valid:
                    references, artifact_errors = self._validate_artifacts(artifacts)
                    terminal = self._stopped(context)
                    if terminal is not None:
                        outcome = self._result(context, terminal, attempts)
                        break
                    if artifact_errors:
                        outcome = self._result(
                            context,
                            "artifact_schema_failure",
                            attempts,
                            details={
                                "error_count": len(artifact_errors),
                                "artifact_index": artifact_errors[0].details["artifact_index"],
                            },
                        )
                        break
                    else:
                        outcome = ExecutionResult(
                            status=ExecutionStatus.SUCCEEDED,
                            request_id=context.request_id,
                            correlation_id=context.correlation_id,
                            trace_id=context.trace_id,
                            output={"data": data, "artifacts": references},
                            metadata=self._metadata(context, attempts),
                        )
                        break
                if repair is None or self._max_repair_attempts == 0:
                    outcome = self._result(
                        context,
                        last.errors[0].code,
                        attempts,
                        details={"error_count": len(last.errors)},
                    )
                    break
                if attempts >= self._max_repair_attempts:
                    outcome = self._result(
                        context,
                        "repair_exhausted",
                        attempts,
                        details={"last_error_code": last.errors[0].code},
                    )
                    break
                attempts += 1
                self._emit(
                    "record_event", f"validation.repair_attempt.{self.schema_id}", context=context
                )
                try:
                    current = await self._repair(repair, current, last, context)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    outcome = self._result(context, "validation_timed_out", attempts)
                    break
                except _RepairCancelled:
                    outcome = self._result(context, "validation_cancelled", attempts)
                    break
                except Exception:
                    if attempts >= self._max_repair_attempts:
                        outcome = self._result(
                            context,
                            "repair_exhausted",
                            attempts,
                            details={"last_error_code": last.errors[0].code},
                        )
                        break
        finally:
            self._emit(
                "record_metric",
                "validation.duration_ms",
                (monotonic() - started) * 1000,
                context=context,
            )
            self._emit("record_metric", "validation.repair_attempts", attempts, context=context)
        succeeded = outcome.status is ExecutionStatus.SUCCEEDED
        event = "validation.success" if succeeded else "validation.failure"
        self._emit("record_event", f"{event}.{self.schema_id}", context=context)
        if attempts:
            repair_event = "validation.repair_success" if succeeded else "validation.repair_failure"
            self._emit("record_event", f"{repair_event}.{self.schema_id}", context=context)
        if outcome.error is not None:
            self._emit("record_error", outcome.error, context=context)
        return outcome

    def _validate_data(
        self, data: dict[str, JsonValue]
    ) -> tuple[dict[str, JsonValue], ValidationResult]:
        try:
            json.dumps(data, allow_nan=False)
        except (TypeError, ValueError):
            return data, ValidationResult(
                valid=False, errors=(_error("schema_violation", self.schema_id),)
            )
        if self._model is not None:
            try:
                parsed = self._model.model_validate(data, strict=True)
                return parsed.model_dump(mode="json"), ValidationResult(valid=True)
            except ValidationError as exc:
                errors = tuple(
                    _error(
                        _pydantic_code(item["type"], len(item["loc"])),
                        self.schema_id,
                        tuple(str(part) for part in item["loc"]),
                    )
                    for item in exc.errors(include_input=False)[:10]
                )
                return data, ValidationResult(valid=False, errors=errors)
            except Exception:
                return data, ValidationResult(
                    valid=False, errors=(_error("schema_violation", self.schema_id),)
                )
        assert self._schema is not None
        try:
            violations = sorted(
                self._schema.iter_errors(data), key=lambda item: str(list(item.path))
            )[:10]
        except Exception:
            return data, ValidationResult(
                valid=False, errors=(_error("schema_violation", self.schema_id),)
            )
        errors = tuple(
            _error(
                _schema_code(str(item.validator), item.validator_value, len(item.path)),
                self.schema_id,
                tuple(item.path),
            )
            for item in violations
        )
        return data, ValidationResult(valid=not errors, errors=errors)

    def _parse_once(self, raw: str) -> tuple[dict[str, JsonValue], ValidationResult]:
        try:
            data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data, dict):
                raise TypeError
            json.dumps(data, allow_nan=False)
        except (json.JSONDecodeError, TypeError, ValueError):
            error = _error("malformed_json", self.schema_id)
            return {}, ValidationResult(valid=False, errors=(error,))
        return self._validate_data(data)

    def _validate_artifacts(
        self, artifacts: Sequence[ArtifactReference | Mapping[str, JsonValue]]
    ) -> tuple[list[dict[str, JsonValue]], list[ExecutionError]]:
        references: list[dict[str, JsonValue]] = []
        errors: list[ExecutionError] = []
        for index, raw in enumerate(artifacts):
            try:
                ref = ArtifactReference.model_validate(raw, strict=True)
                if self._artifact_schema is not None:
                    violations = list(self._artifact_schema.iter_errors(ref.metadata or {}))
                    if violations:
                        raise ValueError("invalid artifact metadata")
                references.append(ref.model_dump(mode="json"))
            except Exception:
                errors.append(_error("artifact_schema_failure", self.schema_id, (index,)))
                if len(errors) >= 10:
                    break
        return references, errors

    async def _repair(
        self, repair: RepairCallback, raw: str, errors: ValidationResult, context: AgentContext
    ) -> str:
        stopped = self._stopped(context)
        if stopped is not None:
            if stopped == "validation_cancelled":
                raise _RepairCancelled
            raise TimeoutError
        task = asyncio.create_task(repair(raw, errors, context))
        cancellation = (
            asyncio.create_task(context.cancellation_event.wait())
            if context.cancellation_event is not None
            else None
        )
        try:
            remaining = (
                (context.deadline - datetime.now(UTC)).total_seconds()
                if context.deadline is not None
                else None
            )
            done, _ = await asyncio.wait(
                {task} | ({cancellation} if cancellation is not None else set()),
                timeout=max(0, remaining) if remaining is not None else None,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation is not None and cancellation in done:
                raise _RepairCancelled
            if task not in done:
                raise TimeoutError
            result = await task
            if not isinstance(result, str):
                raise TypeError("repair must return JSON text")
            return result
        finally:
            for pending in (task, cancellation):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(item for item in (task, cancellation) if item is not None), return_exceptions=True
            )

    @staticmethod
    def _stopped(context: AgentContext) -> str | None:
        if context.cancellation_requested:
            return "validation_cancelled"
        if context.deadline is not None and context.deadline <= datetime.now(UTC):
            return "validation_timed_out"
        return None

    def _metadata(self, context: AgentContext, attempts: int) -> dict[str, JsonValue]:
        return {
            "schema_id": self.schema_id,
            "task_id": context.task_id,
            "repair_attempts": attempts,
        }

    def _result(
        self,
        context: AgentContext,
        code: str,
        attempts: int,
        *,
        details: dict[str, JsonValue] | None = None,
    ) -> ExecutionResult:
        status = (
            ExecutionStatus.CANCELLED
            if code == "validation_cancelled"
            else ExecutionStatus.TIMED_OUT
            if code == "validation_timed_out"
            else ExecutionStatus.FAILED
        )
        error = _error(code, self.schema_id)
        if details:
            error = error.model_copy(update={"details": {**(error.details or {}), **details}})
        return ExecutionResult(
            status=status,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            error=error,
            metadata=self._metadata(context, attempts),
        )

    def _emit(self, method: str, *args: object, context: AgentContext) -> None:
        if self._telemetry is None:
            return
        try:
            getattr(self._telemetry, method)(*args, context=context)
        except Exception:
            pass  # Observability cannot replace the validation outcome.


class _RepairCancelled(Exception):
    pass
