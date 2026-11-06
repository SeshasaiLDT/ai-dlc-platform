"""Durable one-pass workers with explicit resume, safe retries and fenced cancellation."""

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from jsonschema import Draft202012Validator

from ai_dlc.application.agent_harness import ExecutionError, ExecutionStatus, Invocation
from ai_dlc.application.gateway.context import AuthenticatedRuntimeSelection
from ai_dlc.application.orchestrator.service import (
    DispatchLineage,
    DispatchTrackingError,
    SingleAgentRequest,
)
from ai_dlc.application.planning import PlanError
from ai_dlc.application.workflow.executor import _SAFE_CODES, StepResult
from ai_dlc.application.workflow.recovery_models import (
    Attempt,
    DurableStep,
    ExecutionRecord,
    ExecutionRequest,
    RetryPolicy,
    StartExecution,
)
from ai_dlc.application.workflow.recovery_ports import ExecutionRepository


class RecoveryService:
    def __init__(
        self,
        planning,
        orchestrator,
        repository: ExecutionRepository,
        *,
        policy=None,
        clock=lambda: datetime.now(UTC),
        lease_seconds=10,
        poll_seconds=0.1,
    ):
        if not 0 < poll_seconds <= lease_seconds / 3 or not 0 < lease_seconds <= 60:
            raise ValueError("bounded polling must be shorter than lease")
        self.planning, self.orchestrator, self.repository = planning, orchestrator, repository
        self.policy = policy or RetryPolicy()
        self.clock, self.lease_seconds, self.poll_seconds = clock, lease_seconds, poll_seconds

    def start(self, request: StartExecution, principal):
        plan = self.planning.load_for_execution(request.plan_id, request.initiative_id, principal)
        now, execution_id = self.clock(), uuid4().hex
        return self.repository.create(
            ExecutionRecord(
                execution_id=execution_id,
                plan_id=plan.plan_id,
                initiative_id=plan.initiative_id,
                principal_id=principal.subject_id,
                provider=principal.provider,
                idempotency_key=request.idempotency_key,
                request_id=plan.request_id,
                correlation_id=plan.correlation_id,
                trace_id=uuid4().hex,
                created_at=now,
                updated_at=now,
                deadline=now + timedelta(seconds=self.policy.execution_seconds),
                policy=self.policy,
                steps=tuple(
                    DurableStep(
                        step_id=s.step_id,
                        task_id=uuid5(NAMESPACE_URL, f"{execution_id}/{s.step_id}").hex,
                    )
                    for s in plan.steps
                ),
            )
        )

    def status(self, request: ExecutionRequest, principal):
        record = self.repository.get(request.execution_id)
        if (record.initiative_id, record.principal_id, record.provider) != (
            request.initiative_id,
            principal.subject_id,
            principal.provider,
        ):
            raise PlanError("authorization_failed")
        try:
            self.planning.contexts.resolve(
                AuthenticatedRuntimeSelection(
                    principal, record.initiative_id, "runtime", record.correlation_id
                )
            )
        except Exception:
            raise PlanError("authorization_failed") from None
        return record

    async def cancel(self, request, principal):
        self.status(request, principal)
        self.repository.cancel(request.execution_id, self.clock())
        # A live worker observes intent via its bounded persisted-state monitor.
        try:
            return await self.resume(request, principal)
        except PlanError as error:
            if error.code != "execution_busy":
                raise
            return self.status(request, principal)

    def _save(self, record, token):
        # Only the lease holder writes results. Concurrent cancel/heartbeat updates merge intent.
        for _ in range(4):
            current = self.repository.get(record.execution_id)
            if current.claim_token != token or current.lease_until <= self.clock():
                raise PlanError("execution_conflict")
            merged = record.model_copy(
                update={
                    "revision": current.revision,
                    "lease_until": current.lease_until,
                    "cancel_requested": current.cancel_requested or record.cancel_requested,
                    "cancellation": current.cancellation
                    if record.cancellation == "none"
                    else record.cancellation,
                }
            )
            if merged.cancel_requested and merged.state == "succeeded":
                merged = merged.model_copy(update={"cancellation": "completed"})
            try:
                return self.repository.save(merged, token, self.clock())
            except PlanError as error:
                if error.code != "execution_conflict":
                    raise
        raise PlanError("execution_conflict")

    async def resume(self, request: ExecutionRequest, principal):
        record = self.status(request, principal)
        if record.state in {"succeeded", "cancelled"}:
            return record
        # Cancellation remains available to owners whose capability grants were revoked.
        if record.cancel_requested:
            plan = self.planning.repository.get(record.plan_id)
        else:
            plan = self.planning.load_for_execution(record.plan_id, record.initiative_id, principal)
        from ai_dlc.application.planning.models import order_steps

        order_steps(plan.steps, plan.limits)
        order_steps(plan.steps, self.planning.limits)
        if [s.step_id for s in record.steps] != [s.step_id for s in plan.steps]:
            raise PlanError("execution_conflict")
        token = uuid4().hex
        record = self.repository.claim(record.execution_id, token, self.clock(), self.lease_seconds)
        record = self._save(record.model_copy(update={"state": "active"}), token)
        deadline = min(
            record.deadline, record.created_at + timedelta(seconds=self.policy.execution_seconds)
        )
        try:
            for index, step in enumerate(plan.steps):
                current = self.repository.get(record.execution_id)
                record = record.model_copy(update={"cancel_requested": current.cancel_requested})
                item = record.steps[index]
                if item.state in {"succeeded", "cancelled"}:
                    continue
                if item.state == "active":
                    # An abandoned intent might already have caused remote side effects.
                    attempt = item.attempts[-1].model_copy(update={"outcome": "uncertain"})
                    item = item.model_copy(
                        update={"state": "uncertain", "attempts": (*item.attempts[:-1], attempt)}
                    )
                    record = self._set(record, index, item, token)
                if record.cancel_requested:
                    if item.state == "uncertain":
                        record = await self._reconcile(
                            record, index, step, principal, token, cancel=True
                        )
                    else:
                        item = item.model_copy(update={"state": "cancelled"})
                        record = self._set(record, index, item, token)
                    continue
                if item.state == "uncertain":
                    record = await self._reconcile(record, index, step, principal, token)
                    continue
                if self.clock() >= deadline:
                    record = self._set(
                        record,
                        index,
                        item.model_copy(
                            update={
                                "state": "failed",
                                "result": self._error(step, item, "deadline_exceeded"),
                            }
                        ),
                        token,
                    )
                    continue
                if item.state == "failed":
                    last = item.attempts[-1] if item.attempts else None
                    if not last or not last.retry_eligible or self.clock() < last.retry_at:
                        continue
                    if (
                        item.result
                        and item.result.error
                        and item.result.error.code == "remote_execution_failed"
                        and step.capability.value not in self.policy.retry_safe_capabilities
                    ):
                        continue
                    current_backoff = min(
                        300,
                        max(record.policy.backoff_seconds, self.policy.backoff_seconds)
                        * 2 ** (last.number - 1),
                    )
                    if self.clock() < last.finished_at + timedelta(seconds=current_backoff):
                        continue
                if len(item.attempts) >= min(record.policy.max_attempts, self.policy.max_attempts):
                    continue
                previous = {s.step_id: s for s in record.steps}
                blocked = tuple(
                    dep for dep in step.dependencies if previous[dep].state != "succeeded"
                )
                if blocked:
                    record = self._set(
                        record,
                        index,
                        item.model_copy(
                            update={
                                "state": "blocked",
                                "result": self._error(step, item, "dependency_failed", blocked),
                            }
                        ),
                        token,
                    )
                    continue
                if sum(len(s.attempts) for s in record.steps) >= min(
                    plan.limits.max_delegations, self.planning.limits.max_delegations
                ):
                    record = self._set(
                        record,
                        index,
                        item.model_copy(
                            update={
                                "state": "failed",
                                "result": self._error(step, item, "delegation_budget_exceeded"),
                            }
                        ),
                        token,
                    )
                    continue
                attempt = Attempt(
                    number=len(item.attempts) + 1,
                    started_at=self.clock(),
                    deadline=min(
                        deadline, self.clock() + timedelta(seconds=self.orchestrator.timeout)
                    ),
                )
                item = item.model_copy(
                    update={
                        "state": "active",
                        "attempts": (*item.attempts, attempt),
                        "result": None,
                    }
                )
                record = self._set(record, index, item, token)  # intent committed BEFORE dispatch
                inputs = deepcopy(step.invocation.input)
                for key, dependency in step.upstream_outputs.items():
                    if key in inputs:
                        raise PlanError("execution_conflict")
                    inputs[key] = deepcopy(previous[dependency].result.output)
                stop = asyncio.Event()

                def progress(call_id, remote_id, agent_id, endpoint, index=index):
                    nonlocal record
                    active = record.steps[index]
                    updated = active.attempts[-1].model_copy(
                        update={
                            "call_id": call_id,
                            "remote_task_id": remote_id,
                            "agent_id": agent_id,
                            "endpoint": endpoint,
                        }
                    )
                    record = self._set(
                        record,
                        index,
                        active.model_copy(update={"attempts": (*active.attempts[:-1], updated)}),
                        token,
                    )
                    if record.cancel_requested:
                        raise PlanError("execution_cancelled")

                async def monitor(execution_id=record.execution_id, attempt=attempt, stop=stop):
                    while True:
                        current = self.repository.get(execution_id)
                        if current.cancel_requested or self.clock() >= attempt.deadline:
                            stop.set()
                        self.repository.claim(execution_id, token, self.clock(), self.lease_seconds)
                        await asyncio.sleep(self.poll_seconds)

                call = asyncio.create_task(
                    self.orchestrator.dispatch(
                        SingleAgentRequest(
                            initiative_id=record.initiative_id,
                            capability=step.capability,
                            invocation=Invocation(input=inputs, metadata=step.invocation.metadata),
                        ),
                        principal,
                        self._lineage(record, item),
                        cancellation_event=stop,
                        on_progress=progress,
                        deadline=attempt.deadline,
                    )
                )
                watcher = asyncio.create_task(monitor())
                try:
                    done, _ = await asyncio.wait(
                        {call, watcher}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if watcher in done:
                        await watcher  # storage/lease failure must abort dispatch
                    outcome = await call
                finally:
                    for task in (call, watcher):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(call, watcher, return_exceptions=True)
                record = self._finish(record, index, step, outcome.result, outcome.agent_id, token)
                if record.cancel_requested and record.steps[index].state == "uncertain":
                    record = await self._reconcile(
                        record, index, step, principal, token, cancel=True
                    )
            states = {s.state for s in record.steps}
            state = (
                "succeeded"
                if states == {"succeeded"}
                else "uncertain"
                if "uncertain" in states
                else "cancelled"
                if record.cancel_requested
                else "partial"
                if "succeeded" in states
                else "failed"
            )
            cancellation = record.cancellation
            if record.cancel_requested:
                cancellation = (
                    "completed"
                    if state == "succeeded"
                    else "confirmed"
                    if state == "cancelled"
                    else cancellation
                    if cancellation == "unsupported"
                    else "unknown"
                )
            return self._save(
                record.model_copy(
                    update={
                        "state": state,
                        "cancellation": cancellation,
                        "claim_token": None,
                        "lease_until": None,
                    }
                ),
                token,
            )
        except DispatchTrackingError:
            raise PlanError("execution_storage_failed") from None
        except BaseException:
            # Leave the claim/attempt durable on shutdown or storage failure. After lease expiry
            # a new worker can reconcile it; it must never blindly redispatch that intent.
            raise

    def _lineage(self, record, item):
        return DispatchLineage(
            request_id=record.request_id,
            correlation_id=record.correlation_id,
            trace_id=record.trace_id,
            task_id=item.task_id,
            session_id=record.execution_id,
        )

    def _set(self, record, index, item, token):
        items = list(record.steps)
        items[index] = item
        return self._save(record.model_copy(update={"steps": tuple(items)}), token)

    def _error(self, step, item, code, blocked=()):
        return StepResult(
            step_id=step.step_id,
            task_id=item.task_id,
            capability=step.capability,
            status="blocked" if blocked else "failed",
            blocked_dependencies=blocked,
            upstream_sources=dict(step.upstream_outputs),
            error=ExecutionError(code=code, message="Workflow step did not complete"),
        )

    def _finish(self, record, index, step, result, agent_id, token):
        item = record.steps[index]
        attempt = item.attempts[-1]
        success = result.status == ExecutionStatus.SUCCEEDED and Draft202012Validator(
            step.expected_output
        ).is_valid(result.output)
        code = (
            result.error.code
            if result.error and result.error.code in _SAFE_CODES
            else "invalid_output"
        )
        if code == "cancelled" and not record.cancel_requested and self.clock() >= attempt.deadline:
            code = "deadline_exceeded"
        metadata = result.metadata or {}
        remote = metadata.get("remote_task_id") or attempt.remote_task_id
        # Terminal rejection/failure is known. Transport loss, local timeout/cancel or pending
        # response after call intent is uncertain, regardless of a retry-safe declaration.
        uncertain = (
            bool(attempt.call_id)
            and not success
            and code
            in {
                "remote_unavailable",
                "deadline_exceeded",
                "remote_timeout",
                "cancelled",
                "remote_task_pending",
                "protocol_error",
            }
        )
        cancelled = result.status == ExecutionStatus.CANCELLED and code == "remote_cancelled"
        state = (
            "succeeded"
            if success
            else "cancelled"
            if cancelled
            else "uncertain"
            if uncertain
            else "failed"
        )
        safe = code in {"agent_not_found", "remote_rejected"} or (
            code == "remote_execution_failed"
            and step.capability.value in record.policy.retry_safe_capabilities
            and step.capability.value in self.policy.retry_safe_capabilities
        )
        retry = (
            state == "failed"
            and safe
            and len(item.attempts) < min(record.policy.max_attempts, self.policy.max_attempts)
        )
        attempt = attempt.model_copy(
            update={
                "finished_at": self.clock(),
                "outcome": state,
                "remote_task_id": remote,
                "retry_eligible": retry,
                "retry_at": self.clock()
                + timedelta(
                    seconds=min(
                        300,
                        max(record.policy.backoff_seconds, self.policy.backoff_seconds)
                        * 2 ** (attempt.number - 1),
                    )
                )
                if retry
                else None,
            }
        )
        response = StepResult(
            step_id=step.step_id,
            task_id=item.task_id,
            capability=step.capability,
            agent_id=agent_id,
            status="succeeded" if success else "failed",
            output=deepcopy(result.output) if success else None,
            evidence_metadata=deepcopy(result.metadata) if success else None,
            remote_task_id=remote,
            call_id=attempt.call_id,
            upstream_sources=dict(step.upstream_outputs),
            error=None
            if success
            else ExecutionError(code=code, message="Workflow step did not complete"),
        )
        return self._set(
            record,
            index,
            item.model_copy(
                update={
                    "state": state,
                    "result": response,
                    "attempts": (*item.attempts[:-1], attempt),
                }
            ),
            token,
        )

    async def _reconcile(self, record, index, step, principal, token, cancel=False):
        item = record.steps[index]
        attempt = item.attempts[-1]
        if not attempt.remote_task_id:
            return record.model_copy(
                update={"cancellation": "unknown" if cancel else record.cancellation}
            )
        outcome = await self.orchestrator.dispatch(
            SingleAgentRequest(
                initiative_id=record.initiative_id,
                capability=step.capability,
                invocation=Invocation(input={}),
            ),
            principal,
            self._lineage(record, item),
            remote_task_id=attempt.remote_task_id,
            cancel_remote=cancel,
            expected_agent=attempt.agent_id,
            expected_endpoint=attempt.endpoint,
            deadline=self.clock() + timedelta(seconds=self.lease_seconds / 2),
        )
        result = outcome.result
        # A successful cancel RPC can still return an active task; only terminal evidence counts.
        code = result.error.code if result.error else None
        if code in {
            "unsupported_a2a_method",
            "remote_unavailable",
            "authorization_failed",
            "agent_not_found",
            "ambiguous_agent",
            "protocol_error",
            "remote_task_pending",
        }:
            if cancel:
                return record.model_copy(
                    update={
                        "cancellation": "unsupported"
                        if code == "unsupported_a2a_method"
                        else "unknown"
                    }
                )
            return record
        return self._finish(record, index, step, result, outcome.agent_id, token)
