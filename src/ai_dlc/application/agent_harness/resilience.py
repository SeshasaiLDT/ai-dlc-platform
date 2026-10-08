"""Shared retry, error classification, idempotency identity, and safe-resume primitives.

Four concepts are kept separate and never inferred from one another:
retryable failure (an error class), idempotent operation (declared by trusted adapters),
safe-to-replay (derived from the declared safety plus recorded state), and ambiguous
completion (outcome cannot be established, so nothing is replayed automatically).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Protocol

from pydantic import JsonValue

from .models import AgentContext, ExecutionError, ExecutionResult, ExecutionStatus
from .ports import TelemetryProvider

# --- retry policy ---------------------------------------------------------------------------


def _positive(value: float, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff. Defaults (one attempt, no jitter) never retry."""

    max_attempts: int = 1
    base_delay_seconds: float = 0.05
    max_delay_seconds: float = 0.5
    jitter: float = 0.0  # fraction 0..1 by which a delay may be shortened, never lengthened
    retry_on_timeout: bool = False  # timeouts are retried only when explicitly enabled

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")
        _positive(self.base_delay_seconds, "base_delay_seconds")
        _positive(self.max_delay_seconds, "max_delay_seconds")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base_delay_seconds exceeds max_delay_seconds")
        if (
            isinstance(self.jitter, bool)
            or not isinstance(self.jitter, (int, float))
            or not isfinite(self.jitter)
            or not 0 <= self.jitter <= 1
        ):
            raise ValueError("jitter must be between 0 and 1")
        if type(self.retry_on_timeout) is not bool:
            raise TypeError("retry_on_timeout must be a bool")


def backoff_delay(
    policy: RetryPolicy, attempt: int, sample: Callable[[], float] | None = None
) -> float:
    """Delay after zero-based ``attempt``; always within ``max_delay_seconds``."""
    delay = min(policy.max_delay_seconds, policy.base_delay_seconds * 2**attempt)
    if policy.jitter and sample is not None:
        delay *= 1 - policy.jitter * sample()
    return delay


# --- error classification -------------------------------------------------------------------


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    AUTHORIZATION = "authorization"
    APPROVAL_REQUIRED = "approval_required"
    VALIDATION = "validation"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    AMBIGUOUS = "ambiguous"


_E = ErrorClass
_DEFAULT_CODES: dict[str, ErrorClass] = {
    **dict.fromkeys(
        ("provider_unavailable", "remote_unavailable", "rate_limited", "model_overloaded"),
        _E.TRANSIENT,
    ),
    **dict.fromkeys(
        ("tool_timeout", "remote_timeout", "model_timeout", "deadline_exceeded"), _E.TIMEOUT
    ),
    **dict.fromkeys(("unauthorized_operation", "permission_denied"), _E.AUTHORIZATION),
    **dict.fromkeys(("approval_required", "approval_denied"), _E.APPROVAL_REQUIRED),
    **dict.fromkeys(
        (
            "invalid_arguments",
            "invalid_request",
            "malformed_json",
            "missing_required_field",
            "invalid_field_type",
            "invalid_enum_value",
            "invalid_nested_object",
            "unexpected_property",
            "schema_violation",
            "artifact_schema_failure",
            "repair_exhausted",
        ),
        _E.VALIDATION,
    ),
    **dict.fromkeys(
        ("invocation_cancelled", "remote_cancelled", "validation_cancelled", "cancelled"),
        _E.CANCELLATION,
    ),
    **dict.fromkeys(("outcome_unknown", "remote_task_pending"), _E.AMBIGUOUS),
}
_MESSAGES = {
    _E.TRANSIENT: "Operation failed transiently",
    _E.PERMANENT: "Operation failed",
    _E.AUTHORIZATION: "Operation unauthorized",
    _E.APPROVAL_REQUIRED: "Human approval required",
    _E.VALIDATION: "Operation input or output invalid",
    _E.TIMEOUT: "Operation timed out",
    _E.CANCELLATION: "Operation cancelled",
    _E.AMBIGUOUS: "Operation outcome could not be established",
}


class ErrorClassifier:
    """Maps structured result codes to classes. Messages and exception text are never parsed.

    Provider adapters supply ``overrides`` for their own codes; unknown codes are PERMANENT.
    """

    def __init__(self, overrides: Mapping[str, ErrorClass] | None = None) -> None:
        self._table = {**_DEFAULT_CODES}
        for code, error_class in (overrides or {}).items():
            self._table[code.lower()] = ErrorClass(error_class)

    def classify(self, result: ExecutionResult) -> ErrorClass:
        if result.status is ExecutionStatus.CANCELLED:
            return _E.CANCELLATION
        code = result.error.code.lower() if result.error is not None else ""
        known = self._table.get(code)
        if known is not None:
            return known
        return _E.TIMEOUT if result.status is ExecutionStatus.TIMED_OUT else _E.PERMANENT


# --- operation identity and idempotency -----------------------------------------------------


class OperationCategory(StrEnum):
    TOOL_READ = "tool_read"
    TOOL_WRITE = "tool_write"
    MODEL_INVOCATION = "model_invocation"
    AGENT_DELEGATION = "agent_delegation"


class ReplaySafety(StrEnum):
    """Declared by trusted adapter code, never by provider metadata or model output."""

    READ_ONLY = "read_only"  # no side effects; replay is always safe
    IDEMPOTENT_WRITE = "idempotent_write"  # adapter attests the remote enforces the key
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"  # never replayed automatically


_WRITES = (ReplaySafety.IDEMPOTENT_WRITE, ReplaySafety.NON_IDEMPOTENT_WRITE)


@dataclass(frozen=True, slots=True)
class OperationSpec:
    category: OperationCategory
    safety: ReplaySafety = ReplaySafety.READ_ONLY

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", OperationCategory(self.category))
        object.__setattr__(self, "safety", ReplaySafety(self.safety))
        reads_only = self.category in (
            OperationCategory.TOOL_READ,
            OperationCategory.MODEL_INVOCATION,
        )
        if reads_only and self.safety is not ReplaySafety.READ_ONLY:
            raise ValueError(f"{self.category} operations must be read-only")
        if self.category is OperationCategory.TOOL_WRITE and self.safety not in _WRITES:
            raise ValueError("tool writes must declare a write safety")

    @property
    def is_write(self) -> bool:
        return self.safety in _WRITES


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    """Stable identity of one logical operation, constant across attempts and resumes."""

    idempotency_key: str
    category: OperationCategory
    task_id: str
    initiative_id: str
    request_id: str
    correlation_id: str
    fingerprint: str | None = None


def _digest(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def derive_operation_identity(
    context: AgentContext,
    category: OperationCategory,
    operation_name: str,
    logical_key: str,
    payload: Mapping[str, JsonValue] | None = None,
) -> OperationIdentity:
    """Called by trusted agent code, never with values taken from model-generated arguments.

    The key hashes initiative, task, category, operation name and ``logical_key`` (a stable
    step label such as ``"create-issue:1"``). It excludes request IDs so a resumed task keeps
    its key, and excludes the payload so retrying the same step with edited data is detected
    as a fingerprint conflict instead of becoming a second operation. Only digests are kept.
    """
    if not isinstance(context, AgentContext) or context.task_id is None:
        raise ValueError("trusted AgentContext with a task_id required")
    for name, value in (("operation_name", operation_name), ("logical_key", logical_key)):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{name} must be nonblank and at most 200 characters")
    fingerprint = None
    if payload is not None:
        fingerprint = _digest(json.dumps(payload, sort_keys=True, allow_nan=False))
    category = OperationCategory(category)
    key = _digest(context.initiative_id, context.task_id, category, operation_name, logical_key)
    return OperationIdentity(
        idempotency_key=f"idem-{key}",
        category=category,
        task_id=context.task_id,
        initiative_id=context.initiative_id,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        fingerprint=fingerprint,
    )


class OperationState(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"  # definitively not applied; replay eligible
    UNKNOWN = "unknown"  # completion cannot be established; reconcile first


@dataclass(frozen=True, slots=True)
class OperationRecord:
    identity: OperationIdentity
    state: OperationState
    result_ref: str | None = None
    attempts: int = 0


class IdempotencyStore(Protocol):
    """Narrow coordination port. No durable implementation ships with the harness.

    Records are scoped by ``(initiative_id, idempotency_key)``. Implementations must make
    ``begin`` atomic; only an adapter with cross-process atomicity gives cross-process guarantees.

    Conflict contract for every state, including FAILED: if a record exists and
    ``same_operation(record.identity, identity)`` is false, ``begin`` must return
    ``(existing, False)`` without modifying the record. Only a matching FAILED record may be
    re-opened, and its original identity is preserved.
    """

    async def begin(self, identity: OperationIdentity) -> tuple[OperationRecord, bool]:
        """Create or re-open (FAILED) as IN_PROGRESS -> (record, True); else (existing, False)."""
        ...

    async def record(
        self, identity: OperationIdentity, state: OperationState, result_ref: str | None = None
    ) -> OperationRecord: ...


class InMemoryIdempotencyStore:
    """Deterministic, process-local fake. Not durable and not shared across processes."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], OperationRecord] = {}
        self._lock = asyncio.Lock()

    async def begin(self, identity: OperationIdentity) -> tuple[OperationRecord, bool]:
        async with self._lock:
            key = (identity.initiative_id, identity.idempotency_key)
            existing = self._records.get(key)
            if existing is not None and (
                existing.state is not OperationState.FAILED
                or not same_operation(existing.identity, identity)
            ):
                return existing, False
            original = existing.identity if existing is not None else identity
            attempts = existing.attempts if existing is not None else 0
            created = OperationRecord(original, OperationState.IN_PROGRESS, None, attempts + 1)
            self._records[key] = created
            return created, True

    async def record(
        self, identity: OperationIdentity, state: OperationState, result_ref: str | None = None
    ) -> OperationRecord:
        async with self._lock:
            key = (identity.initiative_id, identity.idempotency_key)
            current = self._records[key]
            updated = replace(current, state=state, result_ref=result_ref)
            self._records[key] = updated
            return updated


class ResumeAction(StrEnum):
    SKIP_COMPLETED = "skip_completed"
    REPLAY = "replay"
    AWAIT_IN_PROGRESS = "await_in_progress"
    RECONCILE = "reconcile"
    CONFLICT = "conflict"


def same_operation(recorded: OperationIdentity, incoming: OperationIdentity) -> bool:
    """Same logical operation: key, scope, category and payload fingerprint all match."""
    return (
        recorded.idempotency_key == incoming.idempotency_key
        and recorded.initiative_id == incoming.initiative_id
        and recorded.task_id == incoming.task_id
        and recorded.category is incoming.category
        and recorded.fingerprint == incoming.fingerprint
    )


def resume_action(record: OperationRecord, identity: OperationIdentity) -> ResumeAction:
    """Pure decision from recorded state. It grants no authority: replay re-enters the
    governed operation, which re-checks authorization and approval."""
    if not same_operation(record.identity, identity):
        return ResumeAction.CONFLICT
    return {
        OperationState.COMPLETED: ResumeAction.SKIP_COMPLETED,
        OperationState.FAILED: ResumeAction.REPLAY,
        OperationState.IN_PROGRESS: ResumeAction.AWAIT_IN_PROGRESS,
        OperationState.UNKNOWN: ResumeAction.RECONCILE,
    }[record.state]


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    state: OperationState
    result_ref: str | None = None


Reconciler = Callable[[OperationIdentity], Awaitable[ReconciliationOutcome]]


@dataclass(frozen=True, slots=True)
class AttemptInfo:
    """Passed to the operation so adapters can propagate the trusted key on every attempt."""

    number: int
    idempotency_key: str | None


# --- executor -------------------------------------------------------------------------------


class _Stop(Exception):
    def __init__(self, cls: ErrorClass) -> None:
        self.cls = cls


class ResilientExecutor:
    """Runs one logical operation with classified, bounded retries. Holds no per-call state."""

    def __init__(
        self,
        policies: Mapping[OperationCategory, RetryPolicy] | None = None,
        *,
        classifier: ErrorClassifier | None = None,
        store: IdempotencyStore | None = None,
        reconciler: Reconciler | None = None,
        telemetry: TelemetryProvider | None = None,
        random_sample: Callable[[], float] = random.random,
    ) -> None:
        self._policies = {OperationCategory(k): v for k, v in (policies or {}).items()}
        self._classifier = classifier if classifier is not None else ErrorClassifier()
        self._store = store
        self._reconciler = reconciler
        self._telemetry = telemetry
        self._sample = random_sample

    async def run(
        self,
        operation: Callable[[AttemptInfo], Awaitable[ExecutionResult]],
        *,
        spec: OperationSpec,
        context: AgentContext,
        identity: OperationIdentity | None = None,
    ) -> ExecutionResult:
        if not isinstance(context, AgentContext) or not isinstance(spec, OperationSpec):
            raise TypeError("OperationSpec and trusted AgentContext required")
        if identity is not None and (
            identity.initiative_id != context.initiative_id
            or identity.task_id != context.task_id
            or identity.category is not spec.category
        ):
            return self._fail(context, spec, "identity_scope_mismatch", _E.PERMANENT)
        if spec.is_write:
            if identity is None:
                return self._fail(context, spec, "idempotency_required", _E.PERMANENT)
            if self._store is None:
                return self._fail(context, spec, "idempotency_store_unavailable", _E.PERMANENT)
            early = await self._admit(spec, context, identity)
            if early is not None:
                return early
        key = identity.idempotency_key if identity is not None else None
        policy = self._policies.get(spec.category, RetryPolicy())
        attempt = 0
        while True:
            attempt += 1
            result, cls = await self._attempt(operation, AttemptInfo(attempt, key), spec, context)
            if result.status is ExecutionStatus.SUCCEEDED:
                await self._settle(spec, identity, OperationState.COMPLETED, _ref(result))
                return self._annotate(result, attempt, key)
            delay = self._retry_delay(policy, spec, cls, result, attempt, context)
            if delay is None:
                return await self._finish(result, cls, spec, identity, context, attempt, policy)
            self._emit(spec, f"retry.{cls}", context, backoff=delay, attempt=attempt)
            try:
                await _sleep(delay, context)
            except _Stop as stop:
                code = (
                    "invocation_cancelled" if stop.cls is _E.CANCELLATION else "deadline_exceeded"
                )
                stopped = self._error(context, code, stop.cls)
                return await self._finish(
                    stopped, stop.cls, spec, identity, context, attempt, policy
                )

    # -- admission / settlement ------------------------------------------------------------

    async def _admit(
        self, spec: OperationSpec, context: AgentContext, identity: OperationIdentity
    ) -> ExecutionResult | None:
        record, created = await self._store.begin(identity)
        if created:
            return None
        action = resume_action(record, identity)
        if action is ResumeAction.RECONCILE and self._reconciler is not None:
            outcome = await self._reconciler(identity)
            record = await self._store.record(identity, outcome.state, outcome.result_ref)
            action = resume_action(record, identity)
            if action is ResumeAction.REPLAY:
                _, created = await self._store.begin(identity)
                return (
                    None
                    if created
                    else self._fail(context, spec, "operation_in_progress", _E.PERMANENT)
                )
        if action is ResumeAction.SKIP_COMPLETED:
            self._emit(spec, "deduplicated", context)
            return ExecutionResult(
                status=ExecutionStatus.SUCCEEDED,
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                trace_id=context.trace_id,
                metadata={
                    "deduplicated": True,
                    "idempotency_key": identity.idempotency_key,
                    "result_ref": record.result_ref,
                },
            )
        if action is ResumeAction.RECONCILE:
            self._emit(spec, "reconciliation_required", context)
            return self._error(
                context,
                "outcome_unknown",
                _E.AMBIGUOUS,
                key=identity.idempotency_key,
                details={"reconciliation_required": True},
            )
        code = (
            "idempotency_conflict" if action is ResumeAction.CONFLICT else "operation_in_progress"
        )
        return self._fail(context, spec, code, _E.PERMANENT)

    async def _settle(
        self,
        spec: OperationSpec,
        identity: OperationIdentity | None,
        state: OperationState,
        ref: str | None = None,
    ) -> None:
        if spec.is_write and identity is not None and self._store is not None:
            await self._store.record(identity, state, ref)

    async def _finish(
        self,
        result: ExecutionResult,
        cls: ErrorClass,
        spec: OperationSpec,
        identity: OperationIdentity | None,
        context: AgentContext,
        attempts: int,
        policy: RetryPolicy,
    ) -> ExecutionResult:
        key = identity.idempotency_key if identity is not None else None
        rejected = cls in (_E.AUTHORIZATION, _E.APPROVAL_REQUIRED, _E.VALIDATION)
        if spec.is_write and not rejected:
            definitive = cls is _E.PERMANENT and spec.safety is ReplaySafety.IDEMPOTENT_WRITE
            await self._settle(
                spec, identity, OperationState.FAILED if definitive else OperationState.UNKNOWN
            )
            if not definitive:
                self._emit(spec, "ambiguous_outcome", context)
                status = (
                    ExecutionStatus.CANCELLED
                    if cls is _E.CANCELLATION
                    else ExecutionStatus.TIMED_OUT
                    if cls is _E.TIMEOUT
                    else ExecutionStatus.FAILED
                )
                return self._error(
                    context,
                    "outcome_unknown",
                    _E.AMBIGUOUS,
                    key=key,
                    attempts=attempts,
                    status=status,
                    details={"reconciliation_required": True},
                )
        elif spec.is_write:
            await self._settle(spec, identity, OperationState.FAILED)
        if cls is _E.TRANSIENT or (cls is _E.TIMEOUT and policy.retry_on_timeout):
            if attempts >= policy.max_attempts > 1:
                self._emit(spec, "retry_exhausted", context, attempt=attempts)
        elif cls in (_E.PERMANENT, _E.AUTHORIZATION, _E.APPROVAL_REQUIRED, _E.VALIDATION):
            self._emit(spec, f"permanent_failure.{cls}", context)
        return self._sanitize(result, cls, context, attempts, key)

    # -- attempts --------------------------------------------------------------------------

    async def _attempt(
        self,
        operation: Callable[[AttemptInfo], Awaitable[ExecutionResult]],
        info: AttemptInfo,
        spec: OperationSpec,
        context: AgentContext,
    ) -> tuple[ExecutionResult, ErrorClass]:
        remaining = _remaining(context)
        if context.cancellation_requested:
            return self._error(context, "invocation_cancelled", _E.CANCELLATION), _E.CANCELLATION
        if remaining is not None and remaining <= 0:
            return self._error(context, "deadline_exceeded", _E.TIMEOUT), _E.TIMEOUT
        task = asyncio.ensure_future(operation(info))
        waiters = {task}
        cancel_wait = None
        if context.cancellation_event is not None:
            cancel_wait = asyncio.ensure_future(context.cancellation_event.wait())
            waiters.add(cancel_wait)
        try:
            done, _ = await asyncio.wait(
                waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if task not in done:
                cls = _E.CANCELLATION if context.cancellation_requested else _E.TIMEOUT
                code = "invocation_cancelled" if cls is _E.CANCELLATION else "deadline_exceeded"
                return self._error(context, code, cls), cls
            try:
                raw = task.result()
            except Exception:
                cls = _E.AMBIGUOUS if spec.is_write else _E.PERMANENT
                return self._error(context, "operation_failed", cls), cls
        finally:
            for item in (task, cancel_wait):
                if item is not None and not item.done():
                    item.cancel()
            await asyncio.gather(
                *(i for i in (task, cancel_wait) if i is not None), return_exceptions=True
            )
        if not isinstance(raw, ExecutionResult) or (
            raw.correlation_id != context.correlation_id
            or raw.trace_id != context.trace_id
            or raw.request_id not in (None, context.request_id)
        ):
            return self._error(context, "invalid_result", _E.PERMANENT), _E.PERMANENT
        if raw.status is ExecutionStatus.SUCCEEDED:
            return raw, _E.PERMANENT  # class unused for success
        return raw, self._classifier.classify(raw)

    def _retry_delay(
        self,
        policy: RetryPolicy,
        spec: OperationSpec,
        cls: ErrorClass,
        result: ExecutionResult,
        attempt: int,
        context: AgentContext,
    ) -> float | None:
        """None means do not retry. Only TRANSIENT (and opted-in TIMEOUT on replay-safe
        operations) can be retried; non-idempotent writes never are."""
        if attempt >= policy.max_attempts or spec.safety is ReplaySafety.NON_IDEMPOTENT_WRITE:
            return None
        if cls is _E.TRANSIENT:
            pass
        elif cls is _E.TIMEOUT and policy.retry_on_timeout and not _expired(result):
            pass
        else:
            return None
        delay = backoff_delay(policy, attempt - 1, self._sample)
        hint = result.error.details.get("retry_after_seconds") if result.error.details else None
        if isinstance(hint, (int, float)) and not isinstance(hint, bool) and isfinite(hint):
            if hint > policy.max_delay_seconds:
                return None  # provider asks for longer than policy allows
            delay = max(delay, float(hint))
        remaining = _remaining(context)
        if remaining is not None and remaining <= delay:
            return None
        return delay

    # -- results and telemetry -------------------------------------------------------------

    def _sanitize(
        self,
        result: ExecutionResult,
        cls: ErrorClass,
        context: AgentContext,
        attempts: int,
        key: str | None,
    ) -> ExecutionResult:
        error = ExecutionError(
            code=result.error.code,
            message=_MESSAGES[cls],
            retryable=False,
        )
        return result.model_copy(
            update={
                "error": error,
                "request_id": context.request_id,
                "metadata": _meta(None, attempts, key, cls),
            }
        )

    @staticmethod
    def _annotate(result: ExecutionResult, attempts: int, key: str | None) -> ExecutionResult:
        return result.model_copy(update={"metadata": _meta(result.metadata, attempts, key, None)})

    def _error(
        self,
        context: AgentContext,
        code: str,
        cls: ErrorClass,
        *,
        key: str | None = None,
        attempts: int = 1,
        status: ExecutionStatus | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> ExecutionResult:
        if status is None:
            status = {
                _E.CANCELLATION: ExecutionStatus.CANCELLED,
                _E.TIMEOUT: ExecutionStatus.TIMED_OUT,
            }.get(cls, ExecutionStatus.FAILED)
        return ExecutionResult(
            status=status,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            trace_id=context.trace_id,
            error=ExecutionError(code=code, message=_MESSAGES[cls], details=details),
            metadata=_meta(None, attempts, key, cls),
        )

    def _fail(
        self, context: AgentContext, spec: OperationSpec, code: str, cls: ErrorClass
    ) -> ExecutionResult:
        self._emit(spec, f"permanent_failure.{cls}", context)
        return self._error(context, code, cls)

    def _emit(
        self,
        spec: OperationSpec,
        event: str,
        context: AgentContext,
        *,
        backoff: float | None = None,
        attempt: int | None = None,
    ) -> None:
        if self._telemetry is None:
            return
        prefix = f"resilience.{spec.category}"
        try:
            self._telemetry.record_event(f"{prefix}.{event}", context=context)
            if attempt is not None:
                self._telemetry.record_metric(f"{prefix}.attempts", float(attempt), context=context)
            if backoff is not None:
                self._telemetry.record_metric(f"{prefix}.backoff_seconds", backoff, context=context)
        except Exception:
            pass  # Observability cannot change the operation result.


def _meta(
    base: Mapping[str, JsonValue] | None, attempts: int, key: str | None, cls: ErrorClass | None
) -> dict[str, JsonValue]:
    meta: dict[str, JsonValue] = dict(base) if base else {}
    meta["attempts"] = attempts
    if key is not None:
        meta["idempotency_key"] = key
    if cls is not None:
        meta["error_class"] = str(cls)
    return meta


def _ref(result: ExecutionResult) -> str | None:
    ref = result.metadata.get("result_ref") if result.metadata else None
    return ref if isinstance(ref, str) else None


def _expired(result: ExecutionResult) -> bool:
    return result.error is not None and result.error.code == "deadline_exceeded"


def _remaining(context: AgentContext) -> float | None:
    if context.deadline is None:
        return None
    return (context.deadline - datetime.now(UTC)).total_seconds()


async def _sleep(delay: float, context: AgentContext) -> None:
    """Backoff that stops at cancellation; the caller already checked the deadline fits."""
    event = context.cancellation_event
    if event is None:
        await asyncio.sleep(delay)
    else:
        try:
            await asyncio.wait_for(event.wait(), timeout=delay)
        except TimeoutError:
            pass
        else:
            raise _Stop(_E.CANCELLATION)
    if context.cancellation_requested:
        raise _Stop(_E.CANCELLATION)
    remaining = _remaining(context)
    if remaining is not None and remaining <= 0:
        raise _Stop(_E.TIMEOUT)
