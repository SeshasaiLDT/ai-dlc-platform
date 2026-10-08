"""Offline strict output, artifact, repair, and telemetry contracts."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from ai_dlc.application.agent_harness import (
    ArtifactReference,
    ExecutionStatus,
    Invocation,
    StructuredOutputValidator,
    create_agent_context,
)
from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.domain.identity import InitiativeMembership, Principal


class Detail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ready", "blocked"]
    count: int
    detail: Detail
    note: str | None = None


SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"enum": ["ready", "blocked"]},
        "count": {"type": "integer"},
        "detail": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "required": ["status", "count", "detail"],
    "additionalProperties": False,
}
ARTIFACT_SCHEMA = {
    "type": "object",
    "properties": {"kind": {"enum": ["report"]}},
    "required": ["kind"],
    "additionalProperties": False,
}
VALID = '{"status":"ready","count":2,"detail":{"name":"ok"}}'


def context(**changes):
    authorization = ResolvedAuthorizationContext(
        Principal("user-1", "test"),
        "initiative-1",
        InitiativeMembership("user-1", "initiative-1"),
    )
    fields = dict(
        task_id="task-1",
        authorization=authorization,
        request_id="request-1",
        correlation_id="correlation-1",
        trace_id="trace-1",
    )
    fields.update(changes)
    return create_agent_context(**fields)


def validator(**options):
    return StructuredOutputValidator("impact.v1", model=Output, **options)


def run(coro):
    return asyncio.run(coro)


def test_valid_json_pydantic_and_result_lineage():
    ctx = context()
    result = run(validator().parse(VALID, context=ctx))
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.output == {
        "data": {"status": "ready", "count": 2, "detail": {"name": "ok"}, "note": None},
        "artifacts": [],
    }
    assert result.metadata == {"schema_id": "impact.v1", "task_id": "task-1", "repair_attempts": 0}
    assert (result.request_id, result.correlation_id, result.trace_id) == (
        ctx.request_id,
        ctx.correlation_id,
        ctx.trace_id,
    )
    assert (
        validator()
        .validate(
            Invocation(input={"status": "ready", "count": 2, "detail": {"name": "ok"}}),
            context=ctx,
        )
        .valid
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("{broken", "malformed_json"),
        ('{"status":"ready"}', "missing_required_field"),
        ('{"status":"ready","count":"2","detail":{"name":"ok"}}', "invalid_field_type"),
        ('{"status":"unknown","count":2,"detail":{"name":"ok"}}', "invalid_enum_value"),
        ('{"status":"ready","count":2,"detail":[]}', "invalid_nested_object"),
        ('{"status":"ready","count":2,"detail":{"name":"ok"},"extra":1}', "unexpected_property"),
        ('{"status":"ready","count":NaN,"detail":{"name":"ok"}}', "malformed_json"),
    ],
)
def test_pydantic_failure_codes_are_sanitized(raw, code):
    result = run(validator().parse(raw, context=context()))
    assert result.status is ExecutionStatus.FAILED
    assert result.error.code == code
    assert raw not in result.model_dump_json()
    assert result.output is None


def test_json_schema_and_nested_validation():
    parsed = StructuredOutputValidator("impact.schema", json_schema=SCHEMA)
    assert run(parsed.parse(VALID, context=context())).status is ExecutionStatus.SUCCEEDED
    cases = [
        ('{"status":"ready","count":2,"detail":{}}', "missing_required_field"),
        ('{"status":"ready","count":2,"detail":"bad"}', "invalid_nested_object"),
        ('{"status":"ready","count":"2","detail":{"name":"ok"}}', "invalid_field_type"),
        ('{"status":"ready","count":2,"detail":{"name":"ok","x":1}}', "unexpected_property"),
    ]
    for raw, code in cases:
        assert run(parsed.parse(raw, context=context())).error.code == code


def test_invalid_schema_configuration_and_optional_fields():
    with pytest.raises(ValueError):
        StructuredOutputValidator("bad", json_schema={"type": "unknown"})
    with pytest.raises(ValueError):
        StructuredOutputValidator("bad", json_schema={"$ref": "https://example.invalid"})
    with pytest.raises(ValueError):
        StructuredOutputValidator("bad", model=Output, json_schema=SCHEMA)
    with pytest.raises(ValueError):
        validator(max_repair_attempts=4)
    valid = run(validator().parse(VALID, context=context()))
    assert valid.output["data"]["note"] is None


def test_artifact_reference_validation_and_no_embedded_payload():
    parsed = validator(artifact_metadata_schema=ARTIFACT_SCHEMA)
    artifact = {"artifact_id": "artifact-1", "store_id": "reports", "metadata": {"kind": "report"}}
    result = run(parsed.parse(VALID, context=context(), artifacts=[artifact]))
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.output["artifacts"] == [artifact]
    assert isinstance(ArtifactReference.model_validate(artifact), ArtifactReference)

    for malformed in (
        {"artifact_id": "", "store_id": "reports"},
        {"artifact_id": "artifact-1", "store_id": "reports", "content": "binary"},
        {"artifact_id": "artifact-1", "store_id": "reports", "metadata": {"kind": "other"}},
        {"artifact_id": "artifact-1", "store_id": "reports", "metadata": {"blob": "x" * 16_385}},
    ):
        failed = run(parsed.parse(VALID, context=context(), artifacts=[malformed]))
        assert failed.error.code == "artifact_schema_failure"
        assert failed.error.details["artifact_index"] == 0
        assert failed.output is None

    async def repair(raw, errors, ctx):
        raise AssertionError("artifact references cannot be repaired from model text")

    failed = run(
        validator(artifact_metadata_schema=ARTIFACT_SCHEMA, max_repair_attempts=1).parse(
            VALID,
            context=context(),
            artifacts=[{"artifact_id": "bad", "store_id": "reports"}],
            repair=repair,
        )
    )
    assert failed.error.code == "artifact_schema_failure"
    assert failed.metadata["repair_attempts"] == 0


def test_repair_disabled_by_default_and_successful_bounded_repair():
    called = 0

    async def repair(raw, errors, ctx):
        nonlocal called
        called += 1
        assert errors.errors[0].code == "malformed_json"
        return VALID

    failed = run(validator().parse("bad", context=context(), repair=repair))
    assert failed.error.code == "malformed_json"
    assert called == 0
    succeeded = run(validator(max_repair_attempts=1).parse("bad", context=context(), repair=repair))
    assert succeeded.status is ExecutionStatus.SUCCEEDED
    assert succeeded.metadata["repair_attempts"] == 1
    assert called == 1


def test_repair_exhaustion_never_accepts_invalid_output():
    async def bad_repair(raw, errors, ctx):
        return '{"status":"wrong"}'

    result = run(
        validator(max_repair_attempts=2).parse("bad", context=context(), repair=bad_repair)
    )
    assert result.error.code == "repair_exhausted"
    assert result.error.details["last_error_code"] in {
        "missing_required_field",
        "invalid_enum_value",
    }
    assert result.metadata["repair_attempts"] == 2
    assert result.output is None


def test_cancellation_during_repair_and_external_task_cancellation():
    async def scenario():
        ctx = context()
        started = asyncio.Event()

        async def blocked(raw, errors, context):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            validator(max_repair_attempts=1).parse("bad", context=ctx, repair=blocked)
        )
        await started.wait()
        ctx.cancellation_event.set()
        result = await task
        assert result.status is ExecutionStatus.CANCELLED
        assert result.error.code == "validation_cancelled"

        ctx = context()
        started.clear()
        task = asyncio.create_task(
            validator(max_repair_attempts=1).parse("bad", context=ctx, repair=blocked)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())


def test_expired_deadline_prevents_repair_and_times_out_in_flight():
    expired = context(deadline=datetime.now(UTC) - timedelta(seconds=1))
    assert run(validator().parse(VALID, context=expired)).status is ExecutionStatus.TIMED_OUT
    assert validator().validate(Invocation(input={}), context=expired).errors[0].code == (
        "validation_timed_out"
    )

    cancelled = context()
    cancelled.cancellation_event.set()
    assert validator().validate(Invocation(input={}), context=cancelled).errors[0].code == (
        "validation_cancelled"
    )

    async def scenario():
        ctx = context(deadline=datetime.now(UTC) + timedelta(milliseconds=20))

        async def blocked(raw, errors, context):
            await asyncio.Event().wait()

        result = await validator(max_repair_attempts=1).parse("bad", context=ctx, repair=blocked)
        assert result.status is ExecutionStatus.TIMED_OUT
        assert result.error.code == "validation_timed_out"

    run(scenario())


class Telemetry:
    def __init__(self, broken=False):
        self.events = []
        self.metrics = []
        self.errors = []
        self.broken = broken

    def record_event(self, name, *, context):
        self.events.append((name, context.correlation_id, context.trace_id))
        if self.broken:
            raise RuntimeError("telemetry secret")

    def record_metric(self, name, value, *, context):
        self.metrics.append((name, value))
        if self.broken:
            raise RuntimeError("telemetry secret")

    def record_error(self, error, *, context):
        self.errors.append(error)
        if self.broken:
            raise RuntimeError("telemetry secret")


def test_telemetry_emission_and_failure_isolation():
    telemetry = Telemetry()
    parsed = validator(telemetry=telemetry)
    run(parsed.parse(VALID, context=context()))
    run(parsed.parse("bad", context=context()))
    assert ("validation.success.impact.v1", "correlation-1", "trace-1") in telemetry.events
    assert ("validation.failure.impact.v1", "correlation-1", "trace-1") in telemetry.events
    assert {name for name, _ in telemetry.metrics} == {
        "validation.duration_ms",
        "validation.repair_attempts",
    }
    assert telemetry.errors[0].code == "malformed_json"
    assert "bad" not in str(telemetry.events + telemetry.metrics + telemetry.errors)

    async def repair(raw, errors, ctx):
        return VALID

    repaired = run(
        validator(max_repair_attempts=1, telemetry=telemetry).parse(
            "bad", context=context(), repair=repair
        )
    )
    assert repaired.status is ExecutionStatus.SUCCEEDED
    assert any(name == "validation.repair_success.impact.v1" for name, _, _ in telemetry.events)

    broken = validator(telemetry=Telemetry(broken=True))
    assert run(broken.parse(VALID, context=context())).status is ExecutionStatus.SUCCEEDED
    assert run(broken.parse("bad", context=context())).error.code == "malformed_json"


def test_concurrent_validation_isolation():
    async def scenario():
        parsed = validator(max_repair_attempts=1)

        async def repair(raw, errors, ctx):
            return VALID

        contexts = [
            context(request_id=f"request-{n}", correlation_id=f"correlation-{n}") for n in range(5)
        ]
        results = await asyncio.gather(
            *(
                parsed.parse("bad" if n % 2 else VALID, context=ctx, repair=repair)
                for n, ctx in enumerate(contexts)
            )
        )
        assert [result.request_id for result in results] == [ctx.request_id for ctx in contexts]
        assert [result.metadata["repair_attempts"] for result in results] == [0, 1, 0, 1, 0]

    run(scenario())
