"""Thread-safe local audit and trusted commit handoff stores."""

from threading import Lock

from ai_dlc.application.local_workspace.models import TrustedCommitHandoff
from ai_dlc.application.local_workspace.ports import WorkspaceAuditEvent


class InMemoryCommitHandoffStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._handoffs: list[TrustedCommitHandoff] = []

    def record(self, handoff: TrustedCommitHandoff) -> None:
        with self._lock:
            self._handoffs.append(handoff)

    def approved_commits_for(
        self, *, initiative_id: str, workspace_id: str, task_id: str, principal_id: str
    ) -> frozenset[tuple[str, str]]:
        with self._lock:
            return frozenset(
                (item.repository_id, item.commit_sha)
                for item in self._handoffs
                if item.initiative_id == initiative_id
                and item.workspace_id == workspace_id
                and item.task_id == task_id
                and item.principal_id == principal_id
            )

    @property
    def handoffs(self) -> tuple[TrustedCommitHandoff, ...]:
        with self._lock:
            return tuple(self._handoffs)


class InMemoryWorkspaceAuditSink:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[WorkspaceAuditEvent] = []

    def record(self, event: WorkspaceAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[WorkspaceAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
