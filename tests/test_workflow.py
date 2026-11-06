"""Real persisted planning and A2AClient with an offline SDK transport."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from a2a.client import ClientCallContext
from a2a.types import Artifact, Part, StreamResponse, Task, TaskState, TaskStatus
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value
from pydantic import ValidationError
from test_gateway import USER, A
from test_planning import request, setup, step

from ai_dlc.adapters.orchestrator.host import Dependencies, RuntimeApp
from ai_dlc.application.agent_harness import ExecutionResult, ExecutionStatus
from ai_dlc.application.orchestrator.service import Orchestrator, a2a_transport
from ai_dlc.application.planning import PlanError, PlanLimits
from ai_dlc.application.workflow.executor import ExecutePlanRequest, WorkflowExecutor


def fixture(tmp_path, steps=None, behavior=None):
    planning, _, _ = setup(tmp_path)
    plan = planning.accept(request(steps), USER)
    calls = []
    evidence = {"sources": [{"source_id": "doc-1", "citation": "s3:opaque", "verified": False}]}

    class Client:
        def __init__(self, card):
            self.card = card

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_message(self, wire, *, context):
            lineage = json_format.MessageToDict(wire.metadata)["ai_dlc"]
            data = json_format.MessageToDict(wire.message.parts[0].data)
            calls.append((wire, lineage, data, self.card, context))
            output = deepcopy(evidence)
            if data["input"]:
                output["input"] = data["input"]
            changes = behavior(len(calls), planning) if behavior else {}
            if changes.pop("transport_error", False):
                raise RuntimeError("secret remote diagnostic")
            if changes.pop("block", False):
                await asyncio.Event().wait()
            result = ExecutionResult(
                status=ExecutionStatus.SUCCEEDED,
                request_id=lineage["request_id"],
                correlation_id=lineage["correlation_id"],
                trace_id=lineage["trace_id"],
                output=output,
                metadata={"evidence": {"source_id": "meta-source"}},
            ).model_dump(mode="json")
            result.update(changes)
            yield StreamResponse(
                task=Task(
                    id=f"remote-{len(calls)}",
                    context_id=wire.message.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                    artifacts=[
                        Artifact(
                            artifact_id="result",
                            parts=[Part(data=json_format.ParseDict(result, Value()))],
                        )
                    ],
                )
            )

    dispatcher = Orchestrator(
        planning.registry,
        planning.contexts,
        planning.authorization,
        a2a_transport(call_context_factory=lambda _: ClientCallContext(), client_factory=Client),
    )
    executor = WorkflowExecutor(planning, dispatcher)
    return executor, plan, calls, evidence


def run(executor, plan):
    return asyncio.run(
        executor.execute(
            ExecutePlanRequest(plan_id=plan.plan_id, initiative_id=plan.initiative_id), USER
        )
    )


def test_real_a2a_lineage_binding_and_evidence_aggregation(tmp_path):
    executor, plan, calls, evidence = fixture(tmp_path)
    before = plan.model_dump_json()
    result = run(executor, plan)
    assert result.outcome == "succeeded"
    assert len(calls) == 2
    assert len({s.task_id for s in result.steps}) == 2
    for item, (wire, lineage, _data, card, context) in zip(result.steps, calls, strict=True):
        assert lineage["request_id"] == result.request_id == plan.request_id
        assert lineage["correlation_id"] == result.correlation_id == plan.correlation_id
        assert lineage["trace_id"] == result.trace_id
        assert lineage["session_id"] == result.execution_id
        assert lineage["parent_task_id"] == item.task_id == wire.message.reference_task_ids[0]
        assert lineage["initiative_id"] == plan.initiative_id
        assert item.remote_task_id.startswith("remote-")
        assert item.call_id == wire.message.message_id
        assert item.agent_id == "dynamic"
        assert card.supported_interfaces[0].url == "https://example.invalid/a2a"
        assert context.timeout > 0
        assert item.output["sources"] == evidence["sources"]
        assert item.evidence_metadata["evidence"]["source_id"] == "meta-source"
        assert "authorization" not in lineage
    assert calls[1][2]["input"]["evidence"] == result.steps[0].output
    assert result.steps[1].upstream_sources == {"evidence": "inspect"}
    assert executor.planning.repository.get(plan.plan_id).model_dump_json() == before


def branches():
    return [
        step("a"),
        step("b", deps=("a",), upstream_outputs={"source": "a"}),
        step("c", "verification"),
    ]


def test_partial_failure_blocks_dependents_but_runs_independent_branch(tmp_path):
    executor, plan, calls, _ = fixture(
        tmp_path, branches(), lambda n, _: {"transport_error": n == 1}
    )
    result = run(executor, plan)
    assert result.outcome == "partial_failure"
    results = {s.step_id: s for s in result.steps}
    assert results["a"].error.code == "remote_unavailable"
    assert results["b"].status == "blocked"
    assert results["b"].blocked_dependencies == ("a",)
    assert results["c"].status == "succeeded"
    assert len(calls) == 2
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize(
    "changes,code",
    [({"correlation_id": "wrong"}, "protocol_error"), ({"output": None}, "invalid_output")],
)
def test_malformed_output_or_returned_lineage_is_not_forwarded(tmp_path, changes, code):
    executor, plan, calls, _ = fixture(tmp_path, behavior=lambda *_: changes.copy())
    result = run(executor, plan)
    assert result.outcome == "failed"
    assert result.steps[0].error.code == code
    assert result.steps[1].status == "blocked"
    assert len(calls) == 1


def test_step_expected_output_schema(tmp_path):
    data = step("a")
    data["expected_output"] = {"type": "object", "required": ["absent"]}
    executor, plan, calls, _ = fixture(tmp_path, [data])
    assert run(executor, plan).steps[0].error.code == "invalid_output"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "schema_field,code,dispatched",
    [("input_schema", "invalid_request", False), ("output_schema", "invalid_output", True)],
)
def test_current_manifest_schemas_enforced(tmp_path, schema_field, code, dispatched):
    executor, plan, calls, _ = fixture(tmp_path)
    registry = executor.planning.registry
    original = registry.get("dynamic").manifest.model_dump()
    original[schema_field] = {"type": "object", "required": ["absent"]}
    registry.register(type(registry.get("dynamic").manifest)(**original))
    result = run(executor, plan)
    assert result.steps[0].error.code == code
    assert bool(calls) is dispatched


@pytest.mark.parametrize(
    "mode,code", [("none", "agent_not_found"), ("multiple", "ambiguous_agent")]
)
def test_dispatch_uses_current_registry_and_explicit_selection_errors(tmp_path, mode, code):
    executor, plan, calls, _ = fixture(tmp_path)
    registry = executor.planning.registry
    original = registry.get("dynamic").manifest.model_dump()
    if mode == "multiple":
        original["agent_id"] = "another"
        registry.register(type(registry.get("dynamic").manifest)(**original))
    else:
        executor.orchestrator.registry = SimpleNamespace(discover=lambda **_: ())
    result = run(executor, plan)
    assert result.steps[0].error.code == code
    assert not calls


def revoke(planning):
    memberships = planning.contexts._memberships
    key = (USER.subject_id, A.initiative.id)
    memberships._records[key] = replace(memberships._records[key], enabled=False)


def test_revocation_before_execution_and_after_first_dispatch(tmp_path):
    def behavior(n, planning):
        if n == 1:
            revoke(planning)
        return {}

    executor, plan, calls, _ = fixture(tmp_path, behavior=behavior)
    result = run(executor, plan)
    assert result.outcome == "partial_failure"
    assert result.steps[1].error.code == "authorization_failed"
    assert len(calls) == 1
    with pytest.raises(PlanError, match="authorization_failed"):
        run(executor, plan)


def test_unsaved_cross_initiative_and_request_injection_fail_closed(tmp_path):
    executor, plan, calls, _ = fixture(tmp_path)
    for plan_id, initiative in [("unsaved", plan.initiative_id), (plan.plan_id, "other")]:
        with pytest.raises(PlanError):
            asyncio.run(
                executor.execute(
                    ExecutePlanRequest(plan_id=plan_id, initiative_id=initiative), USER
                )
            )
    with pytest.raises(ValidationError):
        ExecutePlanRequest(plan_id=plan.plan_id, initiative_id=plan.initiative_id, plan=plan)
    assert not calls


def test_tighter_current_bounds_prevent_dispatch(tmp_path):
    executor, plan, calls, _ = fixture(tmp_path)
    executor.planning.limits = PlanLimits(max_delegations=1)
    with pytest.raises(ValueError):
        run(executor, plan)
    assert not calls


def test_runtime_execute_plan_wiring(tmp_path):
    executor, plan, calls, _ = fixture(tmp_path)
    app = RuntimeApp(
        Dependencies(
            SimpleNamespace(authenticate=lambda _: USER),
            executor.orchestrator,
            executor.planning,
            executor,
        )
    )

    async def call():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host"
        ) as c:
            return await c.post(
                "/invocations",
                headers={"Authorization": "Bearer test"},
                json={
                    "operation": "execute_plan",
                    "request": {"plan_id": plan.plan_id, "initiative_id": plan.initiative_id},
                },
            )

    response = asyncio.run(call())
    assert response.status_code == 503
    assert response.json()["error"] == "recovery_not_configured"
    assert not calls


def test_bounded_timeout_marks_failed_and_blocks_dependency(tmp_path):
    executor, plan, calls, _ = fixture(tmp_path, behavior=lambda *_: {"block": True})
    executor.orchestrator.timeout = 0.01
    result = run(executor, plan)
    assert result.outcome == "failed"
    assert result.steps[0].error.code == "deadline_exceeded"
    assert result.steps[1].status == "blocked"
    assert len(calls) == 1


def test_literal_inputs_preserved_and_nested_data_never_expands_plan(tmp_path):
    data = step("b", "verification", ("a",), upstream_outputs={"upstream": "a"})
    data["invocation"] = {"input": {"literal": "keep", "steps": [{"endpoint": "untrusted-data"}]}}
    executor, plan, calls, _ = fixture(tmp_path, [step("a"), data])
    result = run(executor, plan)
    assert result.outcome == "succeeded"
    assert len(calls) == 2
    assert calls[1][2]["input"]["literal"] == "keep"
    assert calls[1][2]["input"]["upstream"] == result.steps[0].output


def test_request_permissions_and_endpoint_are_rejected():
    for extra in (
        {"permissions": ["verification"]},
        {"endpoint": "https://evil"},
        {"result": {"status": "succeeded"}},
    ):
        with pytest.raises(ValidationError):
            ExecutePlanRequest(plan_id="plan", initiative_id="initiative", **extra)
