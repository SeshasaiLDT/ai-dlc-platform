"""Transactional execution admission and fenced, bounded worker leases."""

import sqlite3
from contextlib import closing
from datetime import timedelta

from ai_dlc.application.planning import PlanError
from ai_dlc.application.workflow.recovery_models import ExecutionRecord


class SQLiteExecutionRepository:
    def __init__(self, path):
        if not str(path).strip() or str(path) == ":memory:":
            raise ValueError("durable path required")
        self.path = str(path)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS workflow_executions (
                id TEXT PRIMARY KEY, initiative TEXT NOT NULL, principal TEXT NOT NULL,
                provider TEXT NOT NULL, submission TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(initiative, principal, provider, submission))""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def create(self, record):
        record = ExecutionRecord.model_validate_json(record.model_dump_json())
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM workflow_executions WHERE initiative=? "
                    "AND principal=? AND provider=? AND submission=?",
                    (
                        record.initiative_id,
                        record.principal_id,
                        record.provider,
                        record.idempotency_key,
                    ),
                ).fetchone()
                if row:
                    original = ExecutionRecord.model_validate_json(row[0])
                    if original.plan_id != record.plan_id:
                        raise PlanError("execution_conflict")
                    return original
                db.execute(
                    "INSERT INTO workflow_executions VALUES (?,?,?,?,?,?)",
                    (
                        record.execution_id,
                        record.initiative_id,
                        record.principal_id,
                        record.provider,
                        record.idempotency_key,
                        record.model_dump_json(),
                    ),
                )
            return record
        except (sqlite3.Error, ValueError):
            raise PlanError("execution_storage_failed") from None

    def get(self, execution_id):
        try:
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT payload FROM workflow_executions WHERE id=?", (execution_id,)
                ).fetchone()
            if not row:
                raise PlanError("execution_not_found")
            return ExecutionRecord.model_validate_json(row[0])
        except (sqlite3.Error, ValueError):
            raise PlanError("execution_storage_failed") from None

    def _change(self, execution_id, change):
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM workflow_executions WHERE id=?", (execution_id,)
                ).fetchone()
                if not row:
                    raise PlanError("execution_not_found")
                old = ExecutionRecord.model_validate_json(row[0])
                new = change(old)
                new = ExecutionRecord.model_validate_json(new.model_dump_json())
                db.execute(
                    "UPDATE workflow_executions SET payload=? WHERE id=?",
                    (new.model_dump_json(), execution_id),
                )
            return new
        except (sqlite3.Error, ValueError):
            raise PlanError("execution_storage_failed") from None

    def claim(self, execution_id, token, now, seconds):
        def change(old):
            if old.state in {"succeeded", "cancelled"}:
                raise PlanError("execution_conflict")
            if old.claim_token == token and old.lease_until <= now:
                raise PlanError("execution_conflict")
            if old.claim_token and old.lease_until > now and old.claim_token != token:
                raise PlanError("execution_busy")
            return old.model_copy(
                update={
                    "claim_token": token,
                    "lease_until": now + timedelta(seconds=seconds),
                    "revision": old.revision + 1,
                    "updated_at": now,
                }
            )

        return self._change(execution_id, change)

    def save(self, record, token, now):
        def change(old):
            if (
                old.claim_token != token
                or old.lease_until <= now
                or old.revision != record.revision
            ):
                raise PlanError("execution_conflict")
            transitions = {
                "queued": {"queued", "active"},
                "active": {"active", "succeeded", "partial", "failed", "uncertain", "cancelled"},
                "partial": {"partial", "active"},
                "failed": {"failed", "active"},
                "uncertain": {"uncertain", "active"},
                "succeeded": {"succeeded"},
                "cancelled": {"cancelled"},
            }
            if record.state not in transitions[old.state]:
                raise PlanError("execution_conflict")
            immutable = {
                "execution_id",
                "plan_id",
                "initiative_id",
                "principal_id",
                "provider",
                "idempotency_key",
                "request_id",
                "correlation_id",
                "trace_id",
                "created_at",
                "deadline",
                "policy",
            }
            if any(getattr(old, key) != getattr(record, key) for key in immutable):
                raise PlanError("execution_conflict")
            # Completed outputs cannot be overwritten by stale workers or retries.
            for before, after in zip(old.steps, record.steps, strict=True):
                if before.step_id != after.step_id or before.task_id != after.task_id:
                    raise PlanError("execution_conflict")
                if len(after.attempts) < len(before.attempts):
                    raise PlanError("execution_conflict")
                immutable_count = len(before.attempts) - (
                    1 if before.state in {"active", "uncertain"} else 0
                )
                if after.attempts[:immutable_count] != before.attempts[:immutable_count]:
                    raise PlanError("execution_conflict")
                if before.state == "succeeded" and before != after:
                    raise PlanError("execution_conflict")
                allowed = {
                    "queued": {"queued", "active", "blocked", "cancelled", "failed"},
                    "active": {"active", "succeeded", "failed", "uncertain", "cancelled"},
                    "failed": {"failed", "active", "cancelled"},
                    "blocked": {"blocked", "active", "cancelled", "failed"},
                    "uncertain": {"uncertain", "succeeded", "failed", "cancelled"},
                    "succeeded": {"succeeded"},
                    "cancelled": {"cancelled"},
                }
                if after.state not in allowed[before.state]:
                    raise PlanError("execution_conflict")
            return record.model_copy(
                update={
                    "revision": old.revision + 1,
                    "updated_at": now,
                    "cancel_requested": old.cancel_requested or record.cancel_requested,
                }
            )

        return self._change(record.execution_id, change)

    def cancel(self, execution_id, now):
        def change(old):
            if old.state in {"succeeded", "cancelled"} or old.cancel_requested:
                return old
            return old.model_copy(
                update={
                    "cancel_requested": True,
                    "cancellation": "requested",
                    "updated_at": now,
                    "revision": old.revision + 1,
                }
            )

        return self._change(execution_id, change)
