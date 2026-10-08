"""Offline tests for harness retries, error classification, and idempotency."""

import asyncio
import functools
from datetime import UTC, datetime, timedelta

import pytest

from ai_dlc.application.agent_harness import (
    AgentContext,
    AttemptInfo,
    ErrorClass,
    ErrorClassifier,
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    InMemoryIdempotencyStore,
    OperationCategory,
    OperationSpec,
    OperationState,
    ReconciliationOutcome,
    ReplaySafety,
    ResilientExecutor,
    ResumeAction,
    RetryPolicy,
    backoff_delay,
    create_agent_context,
    derive_operation_identity,
    resume_action,
)
from ai_dlc.application.authorization import ResolvedAuthorizationContext
from ai_dlc.domain.identity import InitiativeMembership, Principal


def sync(test):
    """Run an async test body on a fresh event loop (no async pytest plugin is installed)."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


READ = OperationSpec(OperationCategory.TOOL_READ)
MODEL = OperationSpec(OperationCategory.MODEL_INVOCATION)
IDEM = OperationSpec(OperationCategory.TOOL_WRITE, ReplaySafety.IDEMPOTENT_WRITE)
UNSAFE = OperationSpec(OperationCategory.TOOL_WRITE, ReplaySafety.NON_IDEMPOTENT_WRITE)
FAST = RetryPolicy(max_attempts=3, base_delay_seconds=0.001, max_delay_seconds=0.004)


def ctx(initiative: str = "init-1", task: str = "task-1", **kw: object) -> AgentContext:
    auth = ResolvedAuthorizationContext(
        Principal("user-1", "test"), initiative, InitiativeMembership("user-1", initiative)
    )
    return create_agent_context(task_id=task, authorization=auth, **kw)


def ok(context: AgentContext, **meta: object) -> ExecutionResult:
    return ExecutionResult(
        status=ExecutionStatus.SUCCEEDED,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        output={"data": 1},
        metadata=meta or None,
    )


def bad(
    context: AgentContext,
    code: str,
    status: ExecutionStatus = ExecutionStatus.FAILED,
    **details: object,
) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        request_id=context.request_id,
        correlation_id=context.correlation_id,
        trace_id=context.trace_id,
        error=ExecutionError(
            code=code, message="secret provider text", retryable=True, details=details or None
        ),
    )


class Script:
    """Fake operation returning scripted results and recording attempt info."""

    def __init__(self, context: AgentContext, *steps: object) -> None:
        self.context, self.steps, self.seen = context, list(steps), []

    async def __call__(self, info: AttemptInfo) -> ExecutionResult:
        self.seen.append(info)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, str):
            return ok(self.context) if step == "ok" else bad(self.context, step)
        return step


class Telemetry:
    def __init__(self, fail: bool = False) -> None:
        self.fail, self.events, self.metrics = fail, [], []

    def record_event(self, name: str, *, context: AgentContext) -> None:
        if self.fail:
            raise RuntimeError("down")
        self.events.append(name)

    def record_metric(self, name: str, value: float, *, context: AgentContext) -> None:
        if self.fail:
            raise RuntimeError("down")
        self.metrics.append((name, value))

    def record_error(self, error: ExecutionError, *, context: AgentContext) -> None:
        raise AssertionError("unused")


def executor(**kw: object) -> ResilientExecutor:
    policies = kw.pop("policies", {c: FAST for c in OperationCategory})
    return ResilientExecutor(policies, **kw)


async def run(ex, op, spec=READ, context=None, identity=None):
    context = context or op.context
    return await ex.run(op, spec=spec, context=context, identity=identity)


def identity(context: AgentContext, logical: str = "step-1", payload=None, spec=IDEM):
    return derive_operation_identity(context, spec.category, "jira.create", logical, payload)


@sync
async def test_first_attempt_success_and_default_never_retries() -> None:
    c = ctx()
    op = Script(c, "ok")
    result = await run(executor(), op)
    assert result.status is ExecutionStatus.SUCCEEDED and result.metadata["attempts"] == 1
    op = Script(c, "provider_unavailable", "ok")
    result = await run(ResilientExecutor(), op)
    assert result.error.code == "provider_unavailable" and len(op.seen) == 1


@sync
async def test_transient_retry_then_success_and_exhaustion() -> None:
    c = ctx()
    tel = Telemetry()
    op = Script(c, "provider_unavailable", "rate_limited", "ok")
    result = await run(executor(telemetry=tel), op)
    assert result.status is ExecutionStatus.SUCCEEDED and result.metadata["attempts"] == 3
    assert tel.events.count("resilience.tool_read.retry.transient") == 2
    op = Script(c, "provider_unavailable")
    result = await run(executor(telemetry=tel), op)
    assert len(op.seen) == 3 and result.error.retryable is False  # max attempts enforced
    assert "resilience.tool_read.retry_exhausted" in tel.events


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        ("provider_execution_failed", ErrorClass.PERMANENT),
        ("unauthorized_operation", ErrorClass.AUTHORIZATION),
        ("approval_denied", ErrorClass.APPROVAL_REQUIRED),
        ("schema_violation", ErrorClass.VALIDATION),
        ("malformed_json", ErrorClass.VALIDATION),
    ],
)
@sync
async def test_non_retryable_classes_fail_fast_and_sanitize(code: str, cls: ErrorClass) -> None:
    c = ctx()
    op = Script(c, code, "ok")
    result = await run(executor(), op, spec=MODEL)
    assert len(op.seen) == 1 and result.error.code == code
    assert result.metadata["error_class"] == cls
    assert "secret" not in result.error.message and not result.error.retryable


@sync
async def test_cancellation_and_timeout_not_retried_by_default() -> None:
    c = ctx()
    op = Script(c, bad(c, "invocation_cancelled", ExecutionStatus.CANCELLED), "ok")
    assert len(op.seen) == 0 and (await run(executor(), op)).status is ExecutionStatus.CANCELLED
    assert len(op.seen) == 1
    op = Script(c, bad(c, "tool_timeout", ExecutionStatus.TIMED_OUT), "ok")
    await run(executor(), op)
    assert len(op.seen) == 1  # not every timeout is retryable
    opted = {OperationCategory.TOOL_READ: RetryPolicy(3, 0.001, 0.002, retry_on_timeout=True)}
    op = Script(c, bad(c, "tool_timeout", ExecutionStatus.TIMED_OUT), "ok")
    assert (await run(executor(policies=opted), op)).status is ExecutionStatus.SUCCEEDED


def test_backoff_policy_validation_and_exponential_bounded() -> None:
    p = RetryPolicy(max_attempts=5, base_delay_seconds=0.1, max_delay_seconds=0.5)
    assert [backoff_delay(p, n) for n in range(5)] == [0.1, 0.2, 0.4, 0.5, 0.5]
    jittered = RetryPolicy(3, 0.1, 0.5, jitter=0.5)
    assert backoff_delay(jittered, 1, lambda: 1.0) == pytest.approx(0.1)
    assert backoff_delay(jittered, 1, lambda: 0.0) == pytest.approx(0.2)
    for bad_kw in (
        {"max_attempts": 0},
        {"jitter": 2},
        {"base_delay_seconds": 1, "max_delay_seconds": 0.5},
    ):
        with pytest.raises(ValueError):
            RetryPolicy(**bad_kw)


@sync
async def test_per_category_policies_and_logged_backoff() -> None:
    c = ctx()
    tel = Telemetry()
    policies = {OperationCategory.MODEL_INVOCATION: RetryPolicy(3, 0.001, 0.004)}
    op = Script(c, "provider_unavailable")
    await run(executor(policies=policies, telemetry=tel), op, spec=MODEL)
    assert len(op.seen) == 3
    delays = [v for n, v in tel.metrics if n.endswith("backoff_seconds")]
    assert delays == [0.001, 0.002]
    op = Script(c, "provider_unavailable")
    await run(executor(policies=policies), op, spec=READ)  # read category has default policy
    assert len(op.seen) == 1


@sync
async def test_model_retry_after_guidance_and_malformed_output() -> None:
    c = ctx()
    policy = {OperationCategory.MODEL_INVOCATION: RetryPolicy(2, 0.001, 0.05)}
    tel = Telemetry()
    op = Script(c, bad(c, "rate_limited", retry_after_seconds=0.02), "ok")
    result = await run(executor(policies=policy, telemetry=tel), op, spec=MODEL)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert ("resilience.model_invocation.backoff_seconds", 0.02) in tel.metrics
    op = Script(c, bad(c, "rate_limited", retry_after_seconds=60), "ok")
    assert (await run(executor(policies=policy), op, spec=MODEL)).error.code == "rate_limited"
    assert len(op.seen) == 1
    op = Script(c, "repair_exhausted", "ok")
    await run(executor(policies=policy), op, spec=MODEL)
    assert len(op.seen) == 1
    with pytest.raises(ValueError):
        OperationSpec(OperationCategory.MODEL_INVOCATION, ReplaySafety.IDEMPOTENT_WRITE)


@sync
async def test_deadline_prevents_backoff_and_times_out_attempt() -> None:
    soon = datetime.now(UTC) + timedelta(milliseconds=30)
    c = ctx(deadline=soon)
    policy = {OperationCategory.TOOL_READ: RetryPolicy(3, 5, 10)}
    op = Script(c, "provider_unavailable")
    result = await run(executor(policies=policy), op)
    assert len(op.seen) == 1 and result.error.code == "provider_unavailable"

    async def hang(info: AttemptInfo) -> ExecutionResult:
        await asyncio.sleep(5)

    hang.context = c
    result = await run(executor(), hang)
    assert result.status is ExecutionStatus.TIMED_OUT
    expired = ctx(deadline=datetime.now(UTC) - timedelta(seconds=1))
    assert (await run(executor(), Script(expired, "ok"))).status is ExecutionStatus.TIMED_OUT


@sync
async def test_cancellation_during_backoff_and_inflight() -> None:
    c = ctx()
    policy = {OperationCategory.TOOL_READ: RetryPolicy(3, 5, 10)}
    op = Script(c, "provider_unavailable")
    task = asyncio.create_task(run(executor(policies=policy), op))
    await asyncio.sleep(0.02)
    c.cancellation_event.set()
    result = await asyncio.wait_for(task, 1)
    assert result.status is ExecutionStatus.CANCELLED and len(op.seen) == 1

    c = ctx()

    async def hang(info: AttemptInfo) -> ExecutionResult:
        await asyncio.sleep(5)

    hang.context = c
    task = asyncio.create_task(run(executor(), hang))
    await asyncio.sleep(0.01)
    c.cancellation_event.set()
    assert (await asyncio.wait_for(task, 1)).status is ExecutionStatus.CANCELLED


def test_identity_stable_distinct_and_hashed() -> None:
    c = ctx(request_id="r1")
    a = identity(c, payload={"summary": "token=abc123"})
    again = identity(ctx(request_id="r2"), payload={"summary": "token=abc123"})
    assert a.idempotency_key == again.idempotency_key and a.fingerprint == again.fingerprint
    assert "abc123" not in repr(a)
    assert identity(c, "step-2").idempotency_key != a.idempotency_key
    assert identity(ctx(task="task-2")).idempotency_key != a.idempotency_key
    assert identity(ctx(initiative="init-2")).idempotency_key != a.idempotency_key
    other_cat = derive_operation_identity(
        c, OperationCategory.AGENT_DELEGATION, "jira.create", "step-1"
    )
    assert other_cat.idempotency_key != a.idempotency_key
    assert identity(c, "a:b", spec=IDEM).idempotency_key != identity(c, "a", "b").idempotency_key
    with pytest.raises(ValueError):
        derive_operation_identity(c, OperationCategory.TOOL_WRITE, "op", " ")


@sync
async def test_write_requires_identity_store_and_scope() -> None:
    c = ctx()
    op = Script(c, "ok")
    assert (await run(executor(), op, spec=IDEM)).error.code == "idempotency_required"
    ident = identity(c)
    result = await run(executor(), op, spec=IDEM, identity=ident)
    assert result.error.code == "idempotency_store_unavailable" and not op.seen
    foreign = identity(ctx(initiative="init-2"))
    ex = executor(store=InMemoryIdempotencyStore())
    assert (await run(ex, op, spec=IDEM, identity=foreign)).error.code == "identity_scope_mismatch"
    wrong_cat = derive_operation_identity(c, OperationCategory.AGENT_DELEGATION, "x", "y")
    assert (
        await run(ex, op, spec=IDEM, identity=wrong_cat)
    ).error.code == "identity_scope_mismatch"


@sync
async def test_idempotent_write_retries_with_same_key_and_dedupes_completed() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    ident = identity(c)
    op = Script(c, "provider_unavailable", ok(c, result_ref="JIRA-1"))
    ex = executor(store=store)
    result = await run(ex, op, spec=IDEM, identity=ident)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert {a.idempotency_key for a in op.seen} == {ident.idempotency_key} and len(op.seen) == 2
    tel = Telemetry()
    again = Script(c, "ok")
    result = await run(executor(store=store, telemetry=tel), again, spec=IDEM, identity=ident)
    assert result.metadata["deduplicated"] and result.metadata["result_ref"] == "JIRA-1"
    assert not again.seen and "resilience.tool_write.deduplicated" in tel.events


@sync
async def test_non_idempotent_write_never_retried_and_timeout_is_ambiguous() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    ident = identity(c, spec=UNSAFE)
    tel = Telemetry()
    op = Script(c, "provider_unavailable", "ok")
    result = await run(executor(store=store, telemetry=tel), op, spec=UNSAFE, identity=ident)
    assert len(op.seen) == 1 and result.error.code == "outcome_unknown"
    assert result.error.details == {"reconciliation_required": True}
    assert "resilience.tool_write.ambiguous_outcome" in tel.events
    replay = Script(c, "ok")
    ex = executor(store=store, telemetry=tel)
    result = await run(ex, replay, spec=UNSAFE, identity=ident)
    assert not replay.seen and result.error.code == "outcome_unknown"
    assert "resilience.tool_write.reconciliation_required" in tel.events

    # a local timeout after dispatch never means the write failed
    c2 = ctx(deadline=datetime.now(UTC) + timedelta(milliseconds=30))

    async def hang(info: AttemptInfo) -> ExecutionResult:
        await asyncio.sleep(5)

    hang.context = c2
    store2 = InMemoryIdempotencyStore()
    result = await run(
        executor(store=store2),
        hang,
        spec=IDEM,
        identity=identity(c2),
    )
    assert result.error.code == "outcome_unknown" and result.status is ExecutionStatus.TIMED_OUT
    record, _ = await store2.begin(identity(c2))
    assert record.state is OperationState.UNKNOWN


@sync
async def test_exception_in_write_is_ambiguous_but_read_is_permanent() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    result = await run(
        executor(store=store), Script(c, RuntimeError("boom")), spec=IDEM, identity=identity(c)
    )
    assert result.error.code == "outcome_unknown" and "boom" not in repr(result)
    result = await run(executor(), Script(c, RuntimeError("boom")))
    assert result.error.code == "operation_failed"


@sync
async def test_rejected_write_is_replayable_and_in_progress_not_restarted() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    ident = identity(c)
    denied = Script(c, "unauthorized_operation")
    result = await run(executor(store=store), denied, spec=IDEM, identity=ident)
    assert result.error.code == "unauthorized_operation"
    ok_op = Script(c, "ok")
    result = await run(executor(store=store), ok_op, spec=IDEM, identity=ident)
    assert result.status is ExecutionStatus.SUCCEEDED  # FAILED -> replay re-enters governance
    blocked = identity(c, "step-2")
    await store.begin(blocked)
    again = Script(c, "ok")
    result = await run(executor(store=store), again, spec=IDEM, identity=blocked)
    assert result.error.code == "operation_in_progress" and not again.seen


@sync
async def test_fingerprint_conflict_and_cross_initiative_isolation() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    first = identity(c, payload={"a": 1})
    await run(executor(store=store), Script(c, "ok"), spec=IDEM, identity=first)
    edited = identity(c, payload={"a": 2})
    result = await run(executor(store=store), Script(c, "ok"), spec=IDEM, identity=edited)
    assert result.error.code == "idempotency_conflict"
    c2 = ctx(initiative="init-2")
    same_step = identity(c2, payload={"a": 1})
    op = Script(c2, "ok")
    result = await run(executor(store=store), op, spec=IDEM, identity=same_step)
    assert result.status is ExecutionStatus.SUCCEEDED and op.seen  # not deduped across initiatives


@sync
async def test_reconciliation_resolves_unknown() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    ident = identity(c, spec=UNSAFE)
    await run(executor(store=store), Script(c, "provider_unavailable"), spec=UNSAFE, identity=ident)

    async def remote_says_done(i):
        return ReconciliationOutcome(OperationState.COMPLETED, "REF-9")

    op = Script(c, "ok")
    ex = executor(store=store, reconciler=remote_says_done)
    result = await run(ex, op, spec=UNSAFE, identity=ident)
    assert result.metadata["result_ref"] == "REF-9" and not op.seen

    ident2 = identity(c, "step-2", spec=UNSAFE)
    await run(
        executor(store=store), Script(c, "provider_unavailable"), spec=UNSAFE, identity=ident2
    )

    async def still_unknown(i):
        return ReconciliationOutcome(OperationState.UNKNOWN)

    result = await run(
        executor(store=store, reconciler=still_unknown), op, spec=UNSAFE, identity=ident2
    )
    assert result.error.code == "outcome_unknown" and not op.seen

    ident3 = identity(c, "step-3", spec=UNSAFE)
    await run(
        executor(store=store), Script(c, "provider_unavailable"), spec=UNSAFE, identity=ident3
    )

    async def not_applied(i):
        return ReconciliationOutcome(OperationState.FAILED)

    result = await run(
        executor(store=store, reconciler=not_applied), op, spec=UNSAFE, identity=ident3
    )
    assert result.status is ExecutionStatus.SUCCEEDED and op.seen


@sync
async def test_resume_action_decisions() -> None:
    c = ctx()
    ident = identity(c, payload={"a": 1})
    store = InMemoryIdempotencyStore()
    record, _ = await store.begin(ident)
    assert resume_action(record, ident) is ResumeAction.AWAIT_IN_PROGRESS
    for state, action in [
        (OperationState.COMPLETED, ResumeAction.SKIP_COMPLETED),
        (OperationState.FAILED, ResumeAction.REPLAY),
        (OperationState.UNKNOWN, ResumeAction.RECONCILE),
    ]:
        assert resume_action(await store.record(ident, state, "r"), ident) is action
    other_task = identity(ctx(task="task-9"), payload={"a": 1})
    assert resume_action(record, other_task) is ResumeAction.CONFLICT
    assert resume_action(record, identity(c, payload={"a": 2})) is ResumeAction.CONFLICT


@sync
async def test_concurrent_operation_isolation() -> None:
    store = InMemoryIdempotencyStore()
    ex = executor(store=store)

    async def one(n: int) -> ExecutionResult:
        c = ctx(task=f"task-{n}")
        script = Script(c, "provider_unavailable", "ok") if n % 2 else Script(c, "ok")
        return await run(ex, script, spec=IDEM, identity=identity(c))

    results = await asyncio.gather(*(one(n) for n in range(20)))
    assert all(r.status is ExecutionStatus.SUCCEEDED for r in results)
    assert [r.metadata["attempts"] for r in results] == [2 if n % 2 else 1 for n in range(20)]
    c = ctx(task="shared")
    same = identity(c)
    runs = await asyncio.gather(
        *(run(ex, Script(c, "ok"), spec=IDEM, identity=same) for _ in range(5))
    )
    assert (
        sum(
            1
            for r in runs
            if r.status is ExecutionStatus.SUCCEEDED and not (r.metadata or {}).get("deduplicated")
        )
        == 1
    )


@sync
async def test_classifier_adapter_overrides_and_status_mapping() -> None:
    c = ctx()
    classifier = ErrorClassifier(
        {"VENDOR_BUSY": ErrorClass.TRANSIENT, "vendor_forbidden": ErrorClass.AUTHORIZATION}
    )
    assert classifier.classify(bad(c, "vendor_busy")) is ErrorClass.TRANSIENT
    assert classifier.classify(bad(c, "vendor_forbidden")) is ErrorClass.AUTHORIZATION
    assert classifier.classify(bad(c, "never_seen")) is ErrorClass.PERMANENT
    assert (
        classifier.classify(bad(c, "never_seen", ExecutionStatus.TIMED_OUT)) is ErrorClass.TIMEOUT
    )
    assert classifier.classify(bad(c, "x", ExecutionStatus.CANCELLED)) is ErrorClass.CANCELLATION
    assert classifier.classify(bad(c, "remote_task_pending")) is ErrorClass.AMBIGUOUS
    op = Script(c, "vendor_busy", "ok")
    result = await run(executor(classifier=classifier), op)
    assert result.status is ExecutionStatus.SUCCEEDED


@sync
async def test_lineage_mismatch_and_telemetry_isolation_and_content() -> None:
    c = ctx()
    other = ctx()
    result = await run(executor(), Script(c, ok(other)))
    assert result.error.code == "invalid_result"
    tel = Telemetry(fail=True)
    op = Script(c, "provider_unavailable", "ok")
    assert (await run(executor(telemetry=tel), op)).status is ExecutionStatus.SUCCEEDED
    tel = Telemetry()
    await run(executor(telemetry=tel), Script(c, bad(c, "provider_unavailable", token="s3cret")))
    assert "s3cret" not in repr((tel.events, tel.metrics))
    perm = Telemetry()
    await run(executor(telemetry=perm), Script(c, "unauthorized_operation"))
    assert "resilience.tool_read.permanent_failure.authorization" in perm.events


@sync
async def test_mcp_client_uses_shared_retry_policy() -> None:
    from ai_dlc.application.agent_harness import McpClient
    from ai_dlc.application.agent_harness.mcp import RetryPolicy as McpRetryPolicy

    assert McpRetryPolicy is RetryPolicy
    assert RetryPolicy().max_attempts == 1 and RetryPolicy().jitter == 0
    McpClient((object(),), retry_policy=FAST)


@sync
async def test_failed_record_reopens_only_for_matching_fingerprint() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    original = identity(c, payload={"a": 1})
    await store.begin(original)
    await store.record(original, OperationState.FAILED)
    ex = executor(store=store)
    op = Script(c, "ok")
    result = await run(ex, op, spec=IDEM, identity=identity(c, payload={"a": 1}))
    assert result.status is ExecutionStatus.SUCCEEDED and len(op.seen) == 1


@sync
async def test_failed_record_with_different_fingerprint_is_conflict_and_preserved() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    original = identity(c, payload={"a": 1})
    await store.begin(original)
    await store.record(original, OperationState.FAILED)
    op = Script(c, "ok")
    result = await run(executor(store=store), op, spec=IDEM, identity=identity(c, payload={"a": 2}))
    assert result.error.code == "idempotency_conflict" and not op.seen  # never dispatched
    record, created = await store.begin(identity(c, payload={"a": 3}))  # read-only conflict
    assert created is False and record.identity == original
    assert record.state is OperationState.FAILED and record.attempts == 1
    reopened, created = await store.begin(original)  # original can still legitimately reopen
    assert created is True and reopened.identity == original


@sync
async def test_concurrent_conflicting_fingerprints_admit_exactly_one() -> None:
    c = ctx()
    store = InMemoryIdempotencyStore()
    seed = identity(c, payload={"a": 0})
    await store.begin(seed)
    await store.record(seed, OperationState.FAILED)
    ex = executor(store=store)
    ops = [Script(c, "ok") for _ in range(6)]
    results = await asyncio.gather(
        *(
            run(ex, op, spec=IDEM, identity=identity(c, payload={"a": n % 3}))
            for n, op in enumerate(ops)
        )
    )
    dispatched = [n for n, op in enumerate(ops) if op.seen]
    assert all(ops[n].seen for n in dispatched) and len({n % 3 for n in dispatched}) == 1
    assert all(n % 3 == 0 for n in dispatched)  # only the original fingerprint may run
    assert all(r.error.code == "idempotency_conflict" for n, r in enumerate(results) if n % 3)
    record, created = await store.begin(identity(c, payload={"a": 9}))
    assert not created and record.identity.fingerprint == seed.fingerprint
