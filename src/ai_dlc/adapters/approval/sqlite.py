"""Durable local adapter; production uses the same port with DynamoDB transactions."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from ai_dlc.application.approval.errors import (
    ApprovalAuditError,
    ApprovalConflictError,
    ApprovalNotFoundError,
    ApprovalPersistenceError,
)
from ai_dlc.application.approval.models import ApprovalAuditEvent, ApprovalRecord, ApprovalStatus
from ai_dlc.application.authorization import JiraProjectTarget
from ai_dlc.application.tool_policy import (
    GitOperation,
    GitTarget,
    JiraOperation,
    ServiceNowOperation,
    ServiceNowTarget,
    ToolKind,
    ToolOperationRisk,
)

_OPERATIONS = {
    ToolKind.JIRA: JiraOperation,
    ToolKind.GIT: GitOperation,
    ToolKind.SERVICENOW: ServiceNowOperation,
}


def _target(tool: ToolKind, value: dict):
    kind = {
        ToolKind.JIRA: JiraProjectTarget,
        ToolKind.GIT: GitTarget,
        ToolKind.SERVICENOW: ServiceNowTarget,
    }[tool]
    return kind(**value)


def _record(value: str) -> ApprovalRecord:
    data = json.loads(value)
    data["tool"] = ToolKind(data["tool"])
    data["operation"] = _OPERATIONS[data["tool"]](data["operation"])
    data["risk"] = ToolOperationRisk(data["risk"])
    data["target"] = _target(data["tool"], data["target"])
    data["status"] = ApprovalStatus(data["status"])
    data["requested_at"] = datetime.fromisoformat(data["requested_at"])
    if data["decided_at"] is not None:
        data["decided_at"] = datetime.fromisoformat(data["decided_at"])
    return ApprovalRecord(**data)


def _event(value: str) -> ApprovalAuditEvent:
    data = json.loads(value)
    data["tool"] = ToolKind(data["tool"])
    data["operation"] = _OPERATIONS[data["tool"]](data["operation"])
    data["target"] = _target(data["tool"], data["target"])
    data["to_status"] = ApprovalStatus(data["to_status"])
    if data["from_status"] is not None:
        data["from_status"] = ApprovalStatus(data["from_status"])
    data["occurred_at"] = datetime.fromisoformat(data["occurred_at"])
    return ApprovalAuditEvent(**data)


def _json(value: ApprovalRecord | ApprovalAuditEvent) -> str:
    return json.dumps(asdict(value), default=lambda item: item.isoformat(), sort_keys=True)


class SQLiteApprovalRepository:
    """A file-backed reference adapter, independent of browser/chat sessions."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    initiative_id TEXT NOT NULL,
                    requester_id TEXT NOT NULL,
                    decided_by TEXT,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS approvals_pending
                    ON approvals(initiative_id, status);
                CREATE INDEX IF NOT EXISTS approvals_requester
                    ON approvals(requester_id);
                CREATE INDEX IF NOT EXISTS approvals_approver
                    ON approvals(decided_by);
                CREATE TABLE IF NOT EXISTS approval_history (
                    approval_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (approval_id, version)
                );
                CREATE TABLE IF NOT EXISTS approval_audit (
                    event_id TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE (approval_id, version)
                );
                """
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self._path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, approval_id: str) -> ApprovalRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            raise ApprovalNotFoundError
        return _record(row[0])

    def get_by_request_key(self, request_key: str) -> ApprovalRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM approvals WHERE request_key = ?", (request_key,)
            ).fetchone()
        return _record(row[0]) if row else None

    def commit(
        self,
        record: ApprovalRecord,
        event: ApprovalAuditEvent,
        *,
        expected_version: int | None,
    ) -> ApprovalRecord:
        writing_audit = False
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT payload FROM approvals WHERE approval_id=?", (record.approval_id,)
                ).fetchone()
                current = _record(row[0]) if row else None
                if expected_version is None:
                    if record.version != 1 or current is not None:
                        raise ApprovalConflictError
                    db.execute(
                        "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.approval_id,
                            record.request_key,
                            record.version,
                            record.status.value,
                            record.initiative_id,
                            record.principal_id,
                            record.decided_by,
                            _json(record),
                        ),
                    )
                else:
                    if (
                        current is None
                        or current.version != expected_version
                        or current.status is not ApprovalStatus.PENDING
                        or record.version != expected_version + 1
                        or replace(
                            record,
                            version=current.version,
                            status=current.status,
                            decided_at=current.decided_at,
                            decided_by=current.decided_by,
                        )
                        != current
                    ):
                        raise ApprovalConflictError
                    cursor = db.execute(
                        """UPDATE approvals SET version=?, status=?, decided_by=?, payload=?
                        WHERE approval_id=? AND request_key=? AND version=? AND status='pending'""",
                        (
                            record.version,
                            record.status.value,
                            record.decided_by,
                            _json(record),
                            record.approval_id,
                            record.request_key,
                            expected_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ApprovalConflictError
                if event != ApprovalAuditEvent.for_transition(event.event_id, current, record):
                    raise ValueError("approval audit event does not match transition")
                db.execute(
                    "INSERT INTO approval_history VALUES (?, ?, ?)",
                    (record.approval_id, record.version, _json(record)),
                )
                writing_audit = True
                db.execute(
                    "INSERT INTO approval_audit VALUES (?, ?, ?, ?)",
                    (event.event_id, record.approval_id, record.version, _json(event)),
                )
        except sqlite3.IntegrityError:
            if writing_audit:
                raise ApprovalAuditError from None
            raise ApprovalConflictError from None
        except sqlite3.Error:
            if writing_audit:
                raise ApprovalAuditError from None
            raise ApprovalPersistenceError from None
        return record

    def history(self, approval_id: str) -> tuple[ApprovalRecord, ...]:
        self.get(approval_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM approval_history WHERE approval_id=? ORDER BY version",
                (approval_id,),
            ).fetchall()
        return tuple(_record(row[0]) for row in rows)

    def audit_events(self, approval_id: str) -> tuple[ApprovalAuditEvent, ...]:
        self.get(approval_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM approval_audit WHERE approval_id=? ORDER BY version",
                (approval_id,),
            ).fetchall()
        return tuple(_event(row[0]) for row in rows)

    def _list(self, field: str, value: str, *, pending: bool = False) -> tuple[ApprovalRecord, ...]:
        # Field is selected only from fixed internal call sites.
        query = f"SELECT payload FROM approvals WHERE {field}=?"
        if pending:
            query += " AND status='pending'"
        query += " ORDER BY approval_id"
        with self._connect() as db:
            rows = db.execute(query, (value,)).fetchall()
        return tuple(_record(row[0]) for row in rows)

    def list_pending(self, initiative_id: str) -> tuple[ApprovalRecord, ...]:
        return self._list("initiative_id", initiative_id, pending=True)

    def list_requested_by(self, principal_id: str) -> tuple[ApprovalRecord, ...]:
        return self._list("requester_id", principal_id)

    def list_decided_by(self, principal_id: str) -> tuple[ApprovalRecord, ...]:
        return self._list("decided_by", principal_id)
