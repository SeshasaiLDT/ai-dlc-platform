"""Deterministic in-memory audit sink for tests and local development."""

from threading import Lock

from ai_dlc.application.authorization.decisions import AuthorizationAuditEvent


class InMemoryAuthorizationAuditSink:
    def __init__(self) -> None:
        self._events: list[AuthorizationAuditEvent] = []
        self._lock = Lock()

    def record(self, event: AuthorizationAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[AuthorizationAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
