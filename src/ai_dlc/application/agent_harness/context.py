"""Normalize trusted invocation state into the existing AgentContext contract."""

import asyncio
from collections.abc import Mapping
from datetime import datetime
from uuid import uuid4

from pydantic import JsonValue

from ai_dlc.application.authorization import ResolvedAuthorizationContext

from .models import AgentContext


def create_agent_context(
    *,
    task_id: str,
    authorization: ResolvedAuthorizationContext,
    request_id: str | None = None,
    correlation_id: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    deadline: datetime | None = None,
    cancellation_event: asyncio.Event | None = None,
    metadata: Mapping[str, JsonValue] | None = None,
) -> AgentContext:
    """Called by a trusted boundary, never with identity or task data from agent input.

    Missing request/trace IDs are generated. A stateless invocation uses its
    request ID as the session ID. Correlation defaults to the request ID.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id must be nonblank")
    normalized_request_id = request_id if request_id is not None else uuid4().hex
    return AgentContext(
        request_id=normalized_request_id,
        correlation_id=correlation_id if correlation_id is not None else normalized_request_id,
        session_id=session_id if session_id is not None else normalized_request_id,
        trace_id=trace_id if trace_id is not None else uuid4().hex,
        authorization=authorization,
        deadline=deadline,
        cancellation_event=cancellation_event
        if cancellation_event is not None
        else asyncio.Event(),
        metadata=metadata if metadata is not None else {},
        task_id=task_id,
    )
