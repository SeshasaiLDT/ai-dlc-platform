"""Deterministic offline ServiceNow provider and sanitized audit sink."""

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock

from ai_dlc.application.servicenow.models import (
    AddServiceNowCommentRequest,
    CreateServiceNowRecordRequest,
    GetServiceNowRecordRequest,
    SearchServiceNowRecordsRequest,
    ServiceNowErrorCode,
    ServiceNowJournalResult,
    ServiceNowRecordDetail,
    ServiceNowRecordSummary,
    ServiceNowSearchPage,
    ServiceNowWriteResult,
    UpdateServiceNowRecordRequest,
)
from ai_dlc.application.servicenow.ports import (
    ServiceNowProviderContext,
    ServiceNowProviderFailure,
    ServiceNowToolAuditEvent,
)


@dataclass(frozen=True, slots=True)
class ProviderCall:
    operation: str
    connection_alias: str
    instance_alias: str
    scope_id: str


class InMemoryServiceNowProvider:
    def __init__(self, records: tuple[ServiceNowRecordDetail, ...] = ()) -> None:
        self._records = {record.record_id: record for record in records}
        self.calls: list[ProviderCall] = []
        self.fail_next: ServiceNowErrorCode | None = None

    def _record(self, operation: str, context: ServiceNowProviderContext, scope_id: str) -> None:
        self.calls.append(
            ProviderCall(
                operation,
                context.binding.connection_alias,
                context.binding.instance_alias,
                scope_id,
            )
        )
        if self.fail_next is not None:
            code, self.fail_next = self.fail_next, None
            raise ServiceNowProviderFailure(code)

    def _existing(self, record_id: str) -> ServiceNowRecordDetail:
        try:
            return self._records[record_id]
        except KeyError:
            raise ServiceNowProviderFailure(ServiceNowErrorCode.RESOURCE_NOT_FOUND) from None

    def get_record(
        self, context: ServiceNowProviderContext, request: GetServiceNowRecordRequest
    ) -> ServiceNowRecordDetail:
        self._record("get_record", context, request.scope_id)
        return self._existing(request.record_id)

    def search_records(
        self, context: ServiceNowProviderContext, request: SearchServiceNowRecordsRequest
    ) -> ServiceNowSearchPage:
        self._record("search_records", context, request.scope_id)
        matches = [
            record
            for record in sorted(self._records.values(), key=lambda item: item.record_id)
            if record.scope_id == request.scope_id
            and record.record_type == request.record_type
            and (request.number is None or record.number == request.number)
            and (request.state is None or record.state == request.state)
            and (
                request.text is None
                or request.text.casefold()
                in (record.short_description + " " + (record.description or "")).casefold()
            )
            and (
                request.assignment_group is None
                or record.assignment_group == request.assignment_group
            )
        ]
        try:
            start = int(request.cursor) if request.cursor is not None else 0
        except ValueError:
            raise ServiceNowProviderFailure(
                ServiceNowErrorCode.MALFORMED_UPSTREAM_RESPONSE
            ) from None
        if start < 0:
            raise ServiceNowProviderFailure(ServiceNowErrorCode.MALFORMED_UPSTREAM_RESPONSE)
        page = matches[start : start + request.page_size]
        next_offset = start + len(page)
        has_more = next_offset < len(matches)
        return ServiceNowSearchPage(
            records=tuple(
                ServiceNowRecordSummary.model_validate(
                    record.model_dump(exclude={"kind", "description", "requester_display"})
                )
                for record in page
            ),
            next_cursor=str(next_offset) if has_more else None,
            has_more=has_more,
        )

    def create_record(
        self, context: ServiceNowProviderContext, request: CreateServiceNowRecordRequest
    ) -> ServiceNowWriteResult:
        self._record("create_record", context, request.scope_id)
        serial = len(self._records) + 1
        record_id = f"record-{serial}"
        number = f"{request.record_type.value.upper()}-{serial}"
        self._records[record_id] = ServiceNowRecordDetail(
            record_id=record_id,
            number=number,
            scope_id=request.scope_id,
            record_type=request.record_type,
            short_description=request.short_description,
            description=request.description,
            state="open",
            updated_at=datetime.now(UTC),
        )
        return ServiceNowWriteResult(
            record_id=record_id,
            number=number,
            scope_id=request.scope_id,
            record_type=request.record_type,
            state="open",
        )

    def update_record(
        self, context: ServiceNowProviderContext, request: UpdateServiceNowRecordRequest
    ) -> ServiceNowWriteResult:
        self._record("update_record", context, request.scope_id)
        record = self._existing(request.record_id)
        updated = record.model_copy(
            update={
                "short_description": request.short_description or record.short_description,
                "description": request.description or record.description,
                "state": request.state or record.state,
                "updated_at": datetime.now(UTC),
            }
        )
        self._records[request.record_id] = updated
        return ServiceNowWriteResult(
            record_id=updated.record_id,
            number=updated.number,
            scope_id=updated.scope_id,
            record_type=updated.record_type,
            state=updated.state,
        )

    def add_comment(
        self, context: ServiceNowProviderContext, request: AddServiceNowCommentRequest
    ) -> ServiceNowJournalResult:
        self._record("add_comment", context, request.scope_id)
        record = self._existing(request.record_id)
        return ServiceNowJournalResult(
            record_id=record.record_id,
            scope_id=record.scope_id,
            record_type=record.record_type,
            channel=request.channel,
            entry_id=f"entry-{len(self.calls)}",
            created_at=datetime.now(UTC),
        )


class InMemoryServiceNowToolAuditSink:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[ServiceNowToolAuditEvent] = []

    def record(self, event: ServiceNowToolAuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[ServiceNowToolAuditEvent, ...]:
        with self._lock:
            return tuple(self._events)
