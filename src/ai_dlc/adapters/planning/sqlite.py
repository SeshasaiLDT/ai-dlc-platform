"""File-backed transactional, immutable plan snapshots, following the approval adapter."""

import sqlite3
from contextlib import closing

from ai_dlc.application.planning.models import TaskPlan, submission
from ai_dlc.application.planning.service import PlanError


class SQLitePlanRepository:
    def __init__(self, path):
        if not str(path).strip() or str(path) == ":memory:":
            raise ValueError("durable file path required")
        self.path = str(path)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS task_plans (
                plan_id TEXT PRIMARY KEY, initiative TEXT NOT NULL, principal TEXT NOT NULL,
                provider TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(initiative, principal, provider, request_id))""")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def accept(self, plan: TaskPlan) -> TaskPlan:
        plan = TaskPlan.model_validate_json(plan.model_dump_json())
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM task_plans WHERE initiative=? AND principal=? "
                    "AND provider=? AND request_id=?",
                    (
                        plan.initiative_id,
                        plan.principal_id,
                        plan.identity_provider,
                        plan.request_id,
                    ),
                ).fetchone()
                if row:
                    original = TaskPlan.model_validate_json(row[0])
                    if submission(original) != submission(plan):
                        raise PlanError("plan_conflict")
                    return original
                db.execute(
                    "INSERT INTO task_plans VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        plan.plan_id,
                        plan.initiative_id,
                        plan.principal_id,
                        plan.identity_provider,
                        plan.request_id,
                        plan.model_dump_json(),
                    ),
                )
            return plan  # The transaction has committed before returning eligibility.
        except (sqlite3.Error, ValueError):
            raise PlanError("plan_persistence_failed") from None

    def get(self, plan_id: str) -> TaskPlan:
        try:
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT payload FROM task_plans WHERE plan_id=?", (plan_id,)
                ).fetchone()
            if not row:
                raise PlanError("plan_not_found")
            return TaskPlan.model_validate_json(row[0])
        except (sqlite3.Error, ValueError):
            raise PlanError("plan_persistence_failed") from None
