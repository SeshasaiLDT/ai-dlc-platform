"""Durable recovery using real planning, authorization, A2AClient and controlled SDK tasks."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from a2a.client import ClientCallContext
from a2a.types import Artifact, Part, StreamResponse, Task, TaskState, TaskStatus
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value
from pydantic import ValidationError
from test_gateway import USER
from test_planning import request, setup, step

from ai_dlc.adapters.orchestrator.host import Dependencies, RuntimeApp
from ai_dlc.adapters.workflow.sqlite import SQLiteExecutionRepository
from ai_dlc.application.agent_harness import ExecutionResult, ExecutionStatus
from ai_dlc.application.orchestrator.service import (
    Orchestrator,
    a2a_transport,
)
from ai_dlc.application.planning import PlanError
from ai_dlc.application.workflow.recovery import RecoveryService
from ai_dlc.application.workflow.recovery_models import (
    ExecutionRequest,
    RetryPolicy,
    StartExecution,
)


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Crash(BaseException):
    pass


def fixture(tmp_path, *, steps=None, policy=None):
    planning, _, _ = setup(tmp_path)
    plan = planning.accept(request(steps), USER)
    clock = Clock()
    wire_calls, controls, remote = [], [], {}
    modes = []
    started = asyncio.Event()
    credential = ClientCallContext()
    after_call = None

    def terminal(task_id, wire, state, output=None):
        lineage = json_format.MessageToDict(wire.metadata)["ai_dlc"]
        success = state == TaskState.TASK_STATE_COMPLETED
        result = ExecutionResult(
            status=ExecutionStatus.SUCCEEDED if success else ExecutionStatus.FAILED,
            request_id=lineage["request_id"],
            correlation_id=lineage["correlation_id"],
            trace_id=lineage["trace_id"],
            output=output if success else None,
            error=None if success else {"code": "remote_execution_failed", "message": "SECRET"},
            metadata={"citations": [{"source_id": "source-1", "verified": False}]},
        )
        return Task(
            id=task_id,
            context_id=wire.message.context_id,
            status=TaskStatus(state=state),
            artifacts=[
                Artifact(
                    artifact_id="result",
                    parts=[
                        Part(data=json_format.ParseDict(result.model_dump(mode="json"), Value()))
                    ],
                )
            ],
        )

    class Client:
        def __init__(self, card):
            self.card = card

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_message(self, wire, *, context):
            assert context is credential
            wire_calls.append(wire)
            started.set()
            mode = modes.pop(0) if modes else "success"
            task_id = f"remote-{len(wire_calls)}"
            data = json_format.MessageToDict(wire.message.parts[0].data)["input"]
            output = {"evidence": [{"source_id": "source-1"}], "inputs": data}
            remote[task_id] = terminal(task_id, wire, TaskState.TASK_STATE_COMPLETED, output)
            if mode == "crash":
                raise Crash()
            if mode == "block":
                await asyncio.Event().wait()
            if mode == "pending":
                task = Task(
                    id=task_id,
                    context_id=wire.message.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                )
            elif mode == "rejected":
                task = Task(id=task_id, status=TaskStatus(state=TaskState.TASK_STATE_REJECTED))
            elif mode == "failed":
                task = terminal(task_id, wire, TaskState.TASK_STATE_FAILED)
            else:
                task = remote[task_id]
            if after_call:
                after_call()
            yield StreamResponse(task=task)

        async def get_task(self, req, *, context):
            assert context is credential
            controls.append(("get", req.id))
            return remote[req.id]

        async def cancel_task(self, req, *, context):
            assert context is credential
            controls.append(("cancel", req.id))
            if modes and modes[0] == "unsupported":
                modes.pop(0)
                raise NotImplementedError
            if modes and modes[0] == "control_error":
                modes.pop(0)
                raise RuntimeError("SECRET")
            return Task(id=req.id, status=TaskStatus(state=TaskState.TASK_STATE_CANCELED))

    orchestrator = Orchestrator(
        planning.registry,
        planning.contexts,
        planning.authorization,
        a2a_transport(call_context_factory=lambda _: credential, client_factory=Client),
        clock=clock,
    )
    repo = SQLiteExecutionRepository(tmp_path / "executions.db")
    service = RecoveryService(
        planning, orchestrator, repo, policy=policy, clock=clock, poll_seconds=0.005
    )

    def fresh():
        return RecoveryService(
            planning,
            orchestrator,
            SQLiteExecutionRepository(tmp_path / "executions.db"),
            policy=policy,
            clock=clock,
            poll_seconds=0.005,
        )

    def start(key="start-1"):
        return service.start(
            StartExecution(
                plan_id=plan.plan_id, initiative_id=plan.initiative_id, idempotency_key=key
            ),
            USER,
        )

    def control(record):
        return ExecutionRequest(
            execution_id=record.execution_id, initiative_id=record.initiative_id
        )

    return SimpleNamespace(
        service=service,
        fresh=fresh,
        start=start,
        control=control,
        plan=plan,
        clock=clock,
        modes=modes,
        calls=wire_calls,
        controls=controls,
        started=started,
        planning=planning,
        orchestrator=orchestrator,
        remote=remote,
    )


def resume(f, record):
    return asyncio.run(f.service.resume(f.control(record), USER))


def test_admission_idempotency_fresh_resume_and_success_not_rerun(tmp_path):
    f = fixture(tmp_path)
    admitted = f.start()
    assert admitted.state == "queued" and not f.calls
    assert f.start().execution_id == admitted.execution_id
    f.modes.extend(["success", "rejected"])
    first = resume(f, admitted)
    assert first.state == "partial"
    saved = first.steps[0]
    assert saved.result.output["evidence"][0]["source_id"] == "source-1"
    assert not first.claim_token
    # Backoff survives restart; a resume before it elapses does not call anything.
    assert asyncio.run(f.fresh().resume(f.control(first), USER)).steps[0] == saved
    assert len(f.calls) == 2
    f.clock.advance(3)
    final = asyncio.run(f.fresh().resume(f.control(first), USER))
    assert final.state == "succeeded" and len(f.calls) == 3
    assert final.steps[0] == saved
    assert final.steps[1].task_id == first.steps[1].task_id
    assert len(final.steps[1].attempts) == 2
    for wire in f.calls:
        lineage = json_format.MessageToDict(wire.metadata)["ai_dlc"]
        assert lineage["correlation_id"] == admitted.correlation_id
        assert lineage["trace_id"] == admitted.trace_id
        assert lineage["session_id"] == admitted.execution_id
    assert f.calls[1].message.reference_task_ids == f.calls[2].message.reference_task_ids
    assert f.calls[1].message.message_id != f.calls[2].message.message_id
    assert asyncio.run(f.fresh().resume(f.control(final), USER)) == final
    assert len(f.calls) == 3


def test_conflicting_start_and_strict_requests(tmp_path):
    f = fixture(tmp_path)
    admitted = f.start()
    other = f.planning.accept(request(request_id="other"), USER)
    with pytest.raises(PlanError, match="execution_conflict"):
        f.service.start(
            StartExecution(
                plan_id=other.plan_id, initiative_id=other.initiative_id, idempotency_key="start-1"
            ),
            USER,
        )
    with pytest.raises(ValidationError):
        StartExecution(
            plan_id=f.plan.plan_id,
            initiative_id=f.plan.initiative_id,
            idempotency_key="new",
            result={"state": "succeeded"},
        )
    with pytest.raises(PlanError, match="authorization_failed"):
        f.service.status(
            ExecutionRequest(execution_id=admitted.execution_id, initiative_id="other"), USER
        )


def test_concurrent_claims_and_persisted_cancellation_reach_active_local_work(tmp_path):
    f = fixture(tmp_path)
    f.modes.append("block")
    record = f.start()

    async def run():
        worker = asyncio.create_task(f.service.resume(f.control(record), USER))
        await f.started.wait()
        with pytest.raises(PlanError, match="execution_busy"):
            await f.fresh().resume(f.control(record), USER)
        intent = await f.fresh().cancel(f.control(record), USER)
        assert intent.cancel_requested
        result = await asyncio.wait_for(worker, 2)
        assert result.steps[0].state == "uncertain"
        assert result.steps[1].state == "cancelled"
        assert result.cancellation == "unknown"
        assert len(f.calls) == 1
        again = await f.fresh().cancel(f.control(record), USER)
        assert again.cancel_requested and len(f.calls) == 1

    asyncio.run(run())


def test_known_pending_remote_task_is_reconciled_without_redispatch(tmp_path):
    f = fixture(tmp_path)
    f.modes.append("pending")
    first = resume(f, f.start())
    assert first.steps[0].state == "uncertain"
    assert first.steps[0].attempts[0].remote_task_id == "remote-1"
    final = asyncio.run(f.fresh().resume(f.control(first), USER))
    assert final.state == "succeeded"
    assert len(f.calls) == 2  # first pending call + dependent after reconciliation
    assert f.controls == [("get", "remote-1")]


@pytest.mark.parametrize(
    "mode,outcome",
    [(None, "confirmed"), ("unsupported", "unsupported"), ("control_error", "unknown")],
)
def test_fresh_instance_requests_remote_cancellation_and_exposes_limits(tmp_path, mode, outcome):
    f = fixture(tmp_path)
    f.modes.append("pending")
    first = resume(f, f.start())
    if mode:
        f.modes.append(mode)
    cancelled = asyncio.run(f.fresh().cancel(f.control(first), USER))
    assert cancelled.cancellation == outcome
    assert f.controls == [("cancel", "remote-1")]
    assert len(f.calls) == 1
    if outcome == "confirmed":
        assert cancelled.state == "cancelled"
        assert asyncio.run(f.fresh().cancel(f.control(first), USER)) == cancelled


def test_abandoned_intent_is_uncertain_and_stale_worker_cannot_write(tmp_path):
    f = fixture(tmp_path)
    record = f.start()
    old = f.service.repository.claim(record.execution_id, "old", f.clock(), 1)
    # A worker abandoned a claim before attempt intent: safe to acquire and dispatch.
    f.clock.advance(2)
    with pytest.raises(PlanError, match="execution_conflict"):
        f.service.repository.claim(record.execution_id, "old", f.clock(), 1)
    final = resume(f, old)
    assert final.state == "succeeded"
    with pytest.raises(PlanError, match="execution_conflict"):
        f.service.repository.save(old, "old", f.clock())


def test_crash_after_send_does_not_blindly_redispatch(tmp_path):
    f = fixture(tmp_path)
    f.modes.append("crash")
    record = f.start()
    with pytest.raises(Crash):
        resume(f, record)
    active = f.service.repository.get(record.execution_id)
    assert active.steps[0].state == "active"
    assert active.steps[0].attempts[0].call_id
    f.clock.advance(11)
    restored = asyncio.run(f.fresh().resume(f.control(active), USER))
    assert restored.steps[0].state == "uncertain"
    assert restored.steps[1].state == "blocked"
    assert len(f.calls) == 1


def test_storage_failure_before_send_and_before_outcome_fail_closed(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    record = f.start()
    save = f.service.repository.save

    def fail_checkpoint(record, token, now):
        if any(s.attempts and s.attempts[-1].call_id for s in record.steps):
            raise PlanError("execution_storage_failed")
        return save(record, token, now)

    monkeypatch.setattr(f.service.repository, "save", fail_checkpoint)
    with pytest.raises(PlanError, match="execution_storage_failed"):
        resume(f, record)
    assert not f.calls
    monkeypatch.setattr(f.service.repository, "save", save)
    f.clock.advance(11)
    assert asyncio.run(f.fresh().resume(f.control(record), USER)).state == "uncertain"
    assert not f.calls


def test_retry_bounds_permanent_failure_and_deadline(tmp_path):
    f = fixture(tmp_path, policy=RetryPolicy(max_attempts=2))
    f.modes.extend(["rejected", "rejected"])
    first = resume(f, f.start())
    f.clock.advance(3)
    last = asyncio.run(f.fresh().resume(f.control(first), USER))
    assert len(last.steps[0].attempts) == 2
    f.clock.advance(100)
    asyncio.run(f.fresh().resume(f.control(first), USER))
    assert len(f.calls) == 2


def test_dependency_recovers_after_safe_retry_and_independent_success_is_reused(tmp_path):
    f = fixture(tmp_path, steps=[step("a"), step("b", deps=("a",)), step("c")])
    f.modes.extend(["rejected", "success"])
    first = resume(f, f.start())
    assert [s.state for s in first.steps] == ["failed", "succeeded", "blocked"]
    independent = first.steps[1]
    f.clock.advance(3)
    last = asyncio.run(f.fresh().resume(f.control(first), USER))
    assert last.state == "succeeded" and last.steps[1] == independent
    assert len(f.calls) == 4


def test_revoked_authorization_prevents_resume(tmp_path):
    f = fixture(tmp_path)
    record = f.start()
    memberships = f.planning.contexts._memberships
    key = (USER.subject_id, record.initiative_id)
    memberships._records[key] = replace(memberships._records[key], enabled=False)
    with pytest.raises(PlanError, match="authorization_failed"):
        resume(f, record)
    assert not f.calls


def test_host_operations_and_legacy_execute_are_durable(tmp_path):
    f = fixture(tmp_path)
    app = RuntimeApp(
        Dependencies(
            SimpleNamespace(authenticate=lambda _: USER),
            f.orchestrator,
            f.planning,
            recovery=f.service,
        )
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host"
        ) as c:
            payload = {
                "operation": "execute_plan",
                "request": {"plan_id": f.plan.plan_id, "initiative_id": f.plan.initiative_id},
            }
            first = await c.post(
                "/invocations", headers={"Authorization": "Bearer x"}, json=payload
            )
            second = await c.post(
                "/invocations", headers={"Authorization": "Bearer x"}, json=payload
            )
            assert first.status_code == second.status_code == 200
            assert first.json()["execution_id"] == second.json()["execution_id"]
            assert "claim_token" not in first.json()
            assert len(f.calls) == 2

    asyncio.run(run())


def test_crash_before_outcome_persistence_reconciles_known_remote_result(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    record = f.start()
    save = f.service.repository.save

    def fail_outcome(record, token, now):
        if any(s.state == "succeeded" for s in record.steps):
            raise PlanError("execution_storage_failed")
        return save(record, token, now)

    monkeypatch.setattr(f.service.repository, "save", fail_outcome)
    with pytest.raises(PlanError, match="execution_storage_failed"):
        resume(f, record)
    assert len(f.calls) == 1
    stored = f.fresh().status(f.control(record), USER)
    assert stored.steps[0].attempts[0].remote_task_id == "remote-1"
    f.clock.advance(11)
    final = asyncio.run(f.fresh().resume(f.control(record), USER))
    assert final.state == "succeeded"
    assert len(f.calls) == 2
    assert f.controls == [("get", "remote-1")]


@pytest.mark.parametrize("safe", [False, True])
def test_terminal_failures_only_retry_with_explicit_safe_capability(tmp_path, safe):
    f = fixture(
        tmp_path, policy=RetryPolicy(retry_safe_capabilities={"investigation"} if safe else set())
    )
    f.modes.append("failed")
    first = resume(f, f.start())
    assert "SECRET" not in first.model_dump_json()
    assert first.steps[0].attempts[0].retry_eligible is safe
    f.clock.advance(3)
    last = asyncio.run(f.fresh().resume(f.control(first), USER))
    assert last.state == ("succeeded" if safe else "failed")
    assert len(f.calls) == (3 if safe else 1)


def test_persisted_deadline_prevents_new_work_after_restart(tmp_path):
    f = fixture(tmp_path, policy=RetryPolicy(execution_seconds=5))
    record = f.start()
    f.clock.advance(6)
    result = asyncio.run(f.fresh().resume(f.control(record), USER))
    assert result.state == "failed"
    assert all(s.result.error.code == "deadline_exceeded" for s in result.steps)
    assert not f.calls


def test_cancel_queued_and_completion_race_are_deterministic(tmp_path, monkeypatch):
    f = fixture(tmp_path, steps=[step("a")])
    record = f.start()
    cancelled = asyncio.run(f.fresh().cancel(f.control(record), USER))
    assert cancelled.state == "cancelled" and cancelled.cancellation == "confirmed"
    assert not f.calls
    other = f.start("other")
    dispatch = f.orchestrator.dispatch

    async def race(*args, **kwargs):
        outcome = await dispatch(*args, **kwargs)
        f.service.repository.cancel(other.execution_id, f.clock())
        return outcome

    monkeypatch.setattr(f.orchestrator, "dispatch", race)
    completed = resume(f, other)
    assert completed.state == "succeeded" and completed.cancellation == "completed"
    assert completed.steps[0].result.output


def test_authorization_is_rechecked_between_dispatches(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    record = f.start()
    dispatch = f.orchestrator.dispatch

    async def revoke(*args, **kwargs):
        outcome = await dispatch(*args, **kwargs)
        memberships = f.planning.contexts._memberships
        key = (USER.subject_id, record.initiative_id)
        memberships._records[key] = replace(memberships._records[key], enabled=False)
        return outcome

    monkeypatch.setattr(f.orchestrator, "dispatch", revoke)
    result = resume(f, record)
    assert result.state == "partial"
    assert result.steps[1].result.error.code == "authorization_failed"
    assert len(f.calls) == 1


def test_outer_asyncio_cancellation_leaves_recoverable_intent(tmp_path):
    f = fixture(tmp_path)
    record = f.start()
    f.modes.append("block")

    async def run():
        task = asyncio.create_task(f.service.resume(f.control(record), USER))
        await f.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert f.service.repository.get(record.execution_id).steps[0].state == "active"
    f.clock.advance(11)
    assert asyncio.run(f.fresh().resume(f.control(record), USER)).state == "uncertain"
    assert len(f.calls) == 1


def test_active_timeout_is_uncertain_without_remote_identity(tmp_path):
    f = fixture(tmp_path)
    f.orchestrator.timeout = 0.01
    f.modes.append("block")
    result = resume(f, f.start())
    assert result.state == "uncertain"
    assert result.steps[0].result.error.code == "deadline_exceeded"
    assert result.steps[0].attempts[0].retry_eligible is False
    assert len(f.calls) == 1


def test_storage_unavailable_prevents_admission(tmp_path):
    f = fixture(tmp_path)
    f.service.repository.path = str(tmp_path / "missing" / "execution.db")
    with pytest.raises(PlanError, match="execution_storage_failed"):
        f.start()
    assert not f.calls


def test_stale_revision_and_illegal_state_transition_are_rejected(tmp_path):
    f = fixture(tmp_path)
    record = f.start()
    owned = f.service.repository.claim(record.execution_id, "worker", f.clock(), 10)
    cancelled = f.service.repository.cancel(record.execution_id, f.clock())
    with pytest.raises(PlanError, match="execution_conflict"):
        f.service.repository.save(owned, "worker", f.clock())
    invalid = cancelled.model_copy(update={"plan_id": "tampered"})
    with pytest.raises(PlanError, match="execution_conflict"):
        f.service.repository.save(invalid, "worker", f.clock())


def test_http_start_status_cancel_and_resume_strict_operations(tmp_path):
    f = fixture(tmp_path)
    app = RuntimeApp(
        Dependencies(
            SimpleNamespace(authenticate=lambda _: USER),
            f.orchestrator,
            f.planning,
            recovery=f.service,
        )
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://host",
            headers={"Authorization": "Bearer x"},
        ) as c:
            response = await c.post(
                "/invocations",
                json={
                    "operation": "start_execution",
                    "request": {
                        "plan_id": f.plan.plan_id,
                        "initiative_id": f.plan.initiative_id,
                        "idempotency_key": "http",
                    },
                },
            )
            assert response.status_code == 200 and response.json()["state"] == "queued"
            control = {
                "execution_id": response.json()["execution_id"],
                "initiative_id": f.plan.initiative_id,
            }
            for operation in ("execution_status", "cancel_execution", "resume_execution"):
                response = await c.post(
                    "/invocations", json={"operation": operation, "request": control}
                )
                assert response.status_code == 200
            assert response.json()["state"] == "cancelled" and not f.calls

    asyncio.run(run())


def test_concurrent_starts_share_one_durable_execution(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    f = fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: f.start(), range(4)))
    assert len({r.execution_id for r in records}) == 1
    assert not f.calls


def test_cancellation_requires_owner_and_active_initiative(tmp_path):
    from ai_dlc.domain.identity import Principal

    f = fixture(tmp_path)
    record = f.start()
    with pytest.raises(PlanError, match="authorization_failed"):
        asyncio.run(f.service.cancel(f.control(record), Principal("intruder", "test")))
    assert not f.service.repository.get(record.execution_id).cancel_requested
    assert not f.calls


def test_retries_consume_total_stored_delegation_budget(tmp_path):
    from ai_dlc.application.planning import PlanLimits

    f = fixture(tmp_path)
    f.planning.limits = PlanLimits(max_delegations=2)
    f.modes.append("rejected")
    first = resume(f, f.start())
    f.clock.advance(3)
    last = asyncio.run(f.fresh().resume(f.control(first), USER))
    assert len(f.calls) == 2
    assert last.steps[1].result.error.code == "delegation_budget_exceeded"


def test_current_retry_policy_can_remove_eligibility_or_increase_backoff(tmp_path):
    f = fixture(tmp_path, policy=RetryPolicy(retry_safe_capabilities={"investigation"}))
    f.modes.append("failed")
    first = resume(f, f.start())
    f.clock.advance(3)
    service = f.fresh()
    service.policy = RetryPolicy()
    asyncio.run(service.resume(f.control(first), USER))
    assert len(f.calls) == 1
    service.policy = RetryPolicy(backoff_seconds=20, retry_safe_capabilities={"investigation"})
    asyncio.run(service.resume(f.control(first), USER))
    assert len(f.calls) == 1
    f.clock.advance(20)
    assert asyncio.run(service.resume(f.control(first), USER)).state == "succeeded"
