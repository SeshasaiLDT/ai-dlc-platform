"""Governed ServiceNow execution using existing policy, approval, and binding services."""

from collections.abc import Mapping
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from ai_dlc.application.approval import ApprovalNotFoundError, ApprovalService
from ai_dlc.application.authorization import ScopeRestriction
from ai_dlc.application.resource_bindings import (
    BindingNotFoundError,
    BindingResolutionDeniedError,
    LogicalResourceRef,
    ResourceAccess,
    ResourceBindingRegistry,
    ResourceType,
    ServiceNowBinding,
)
from ai_dlc.application.tool_policy import (
    ServiceNowOperation,
    ServiceNowTarget,
    ToolPolicyEffect,
    ToolPolicyReason,
    ToolPolicyRequest,
    ToolPolicyService,
)

from .models import (
    REQUEST_TYPES,
    AddServiceNowCommentRequest,
    CreateServiceNowRecordRequest,
    GetServiceNowRecordRequest,
    SearchServiceNowRecordsRequest,
    ServiceNowData,
    ServiceNowErrorCode,
    ServiceNowJournalResult,
    ServiceNowRecordDetail,
    ServiceNowSearchPage,
    ServiceNowToolError,
    ServiceNowToolResult,
    ServiceNowWriteResult,
    TrustedServiceNowContext,
    UpdateServiceNowRecordRequest,
)
from .ports import (
    ServiceNowProvider,
    ServiceNowProviderContext,
    ServiceNowProviderFailure,
    ServiceNowToolAuditEvent,
    ServiceNowToolAuditSink,
)


class ServiceNowAuditError(Exception):
    """An execution result cannot be returned without its audit event."""


class ServiceNowCancelled(Exception):
    """Trusted cancellation signal observed by an adapter."""


_MESSAGES = {
    ServiceNowErrorCode.PERMISSION_DENIED: "ServiceNow operation is not permitted",
    ServiceNowErrorCode.INVALID_SCOPE: "ServiceNow scope is outside the selected initiative",
    ServiceNowErrorCode.INVALID_ARGUMENT: "Invalid ServiceNow operation arguments",
    ServiceNowErrorCode.RESOURCE_NOT_FOUND: "ServiceNow record was not found",
    ServiceNowErrorCode.UPSTREAM_AUTH_CONFIGURATION: "ServiceNow connection is unavailable",
    ServiceNowErrorCode.UPSTREAM_TIMEOUT: "ServiceNow operation timed out",
    ServiceNowErrorCode.MALFORMED_UPSTREAM_RESPONSE: "ServiceNow returned an invalid response",
    ServiceNowErrorCode.TRANSIENT_UPSTREAM_FAILURE: "ServiceNow is temporarily unavailable",
    ServiceNowErrorCode.CANCELLED: "ServiceNow operation was cancelled",
}


class GovernedServiceNowService:
    def __init__(
        self,
        tool_policy: ToolPolicyService,
        bindings: ResourceBindingRegistry,
        provider: ServiceNowProvider,
        audit_sink: ServiceNowToolAuditSink,
        *,
        approvals: ApprovalService | None = None,
    ) -> None:
        self._tool_policy = tool_policy
        self._bindings = bindings
        self._provider = provider
        self._audit = audit_sink
        self._approvals = approvals

    def invoke(
        self,
        operation: ServiceNowOperation,
        arguments: Mapping[str, object],
        *,
        context: TrustedServiceNowContext,
    ) -> ServiceNowToolResult:
        if not isinstance(operation, ServiceNowOperation) or not isinstance(
            context, TrustedServiceNowContext
        ):
            raise TypeError("typed ServiceNow operation and trusted context required")
        started = perf_counter()
        audit_ref = uuid4().hex
        request = None
        decision_id = None
        code: ServiceNowErrorCode | None = None
        data: ServiceNowData | None = None
        try:
            request_type = REQUEST_TYPES.get(operation)
            if request_type is None or not isinstance(arguments, Mapping):
                code = ServiceNowErrorCode.INVALID_ARGUMENT
            else:
                try:
                    request = request_type.model_validate(arguments)
                except (ValidationError, ValueError, TypeError):
                    code = ServiceNowErrorCode.INVALID_ARGUMENT
            if code is None and context.cancelled:
                code = ServiceNowErrorCode.CANCELLED
            if code is None and request is not None:
                if not self._scope_allowed(request, context):
                    code = ServiceNowErrorCode.INVALID_SCOPE
                else:
                    policy_request = ToolPolicyRequest(
                        context.resource_context.authorization.principal,
                        context.resource_context.authorization.initiative_id,
                        operation,
                        ServiceNowTarget(request.scope_id),
                    )
                    restriction = ScopeRestriction(
                        servicenow_scopes=context.resource_context.authorization.allowed_scopes.servicenow_scopes
                    )
                    try:
                        decision = self._tool_policy.evaluate(
                            policy_request,
                            profile=context.resource_context.profile,
                            initiative_revision=context.initiative_revision,
                            scope_restriction=restriction,
                        )
                        decision_id = decision.decision_id
                        if decision.effect is ToolPolicyEffect.DENY:
                            code = (
                                ServiceNowErrorCode.INVALID_SCOPE
                                if decision.reason is ToolPolicyReason.TARGET_NOT_ALLOWED
                                else ServiceNowErrorCode.PERMISSION_DENIED
                            )
                        elif decision.effect is ToolPolicyEffect.REQUIRE_APPROVAL:
                            if (
                                self._approvals is None
                                or context.approval_id is None
                                or not self._approvals.approval_gate_satisfied(
                                    context.approval_id,
                                    policy_request,
                                    profile=context.resource_context.profile,
                                    initiative_revision=context.initiative_revision,
                                    scope_restriction=restriction,
                                )
                            ):
                                code = ServiceNowErrorCode.PERMISSION_DENIED
                    except ApprovalNotFoundError:
                        code = ServiceNowErrorCode.PERMISSION_DENIED
                    except Exception:
                        code = ServiceNowErrorCode.UPSTREAM_AUTH_CONFIGURATION
                    if code is None:
                        try:
                            resolved = self._bindings.resolve(
                                LogicalResourceRef(
                                    ResourceType.SERVICENOW, "servicenow", request.scope_id
                                ),
                                context=context.resource_context,
                                access=ResourceAccess.READ
                                if operation
                                in (ServiceNowOperation.READ_RECORD, ServiceNowOperation.SEARCH)
                                else ResourceAccess.WRITE,
                            )
                            binding = resolved.binding.details
                            if not isinstance(binding, ServiceNowBinding):
                                raise ValueError("ServiceNow binding has wrong type")
                            provider_context = ServiceNowProviderContext(
                                binding,
                                context.resource_context.correlation_id,
                                context.timeout_seconds,
                            )
                            if operation in (
                                ServiceNowOperation.UPDATE_RECORD,
                                ServiceNowOperation.ADD_COMMENT,
                            ):
                                current = self._provider.get_record(
                                    provider_context,
                                    GetServiceNowRecordRequest(
                                        scope_id=request.scope_id,
                                        record_type=request.record_type,
                                        record_id=request.record_id,
                                    ),
                                )
                                self._normalize(
                                    ServiceNowOperation.READ_RECORD, request, current, context
                                )
                            raw = self._dispatch(operation, provider_context, request)
                            data = self._normalize(operation, request, raw, context)
                        except (BindingNotFoundError, BindingResolutionDeniedError):
                            code = ServiceNowErrorCode.UPSTREAM_AUTH_CONFIGURATION
                        except ServiceNowProviderFailure as exc:
                            code = exc.code
                        except TimeoutError:
                            code = ServiceNowErrorCode.UPSTREAM_TIMEOUT
                        except ServiceNowCancelled:
                            code = ServiceNowErrorCode.CANCELLED
                        except (ValidationError, ValueError, TypeError):
                            code = ServiceNowErrorCode.MALFORMED_UPSTREAM_RESPONSE
                        except Exception:
                            code = ServiceNowErrorCode.TRANSIENT_UPSTREAM_FAILURE
        except Exception:
            code = ServiceNowErrorCode.UPSTREAM_AUTH_CONFIGURATION
        finally:
            event = ServiceNowToolAuditEvent(
                audit_ref=audit_ref,
                occurred_at=datetime.now(UTC),
                correlation_id=context.resource_context.correlation_id,
                principal_id=context.resource_context.authorization.principal.subject_id,
                initiative_id=context.resource_context.authorization.initiative_id,
                workspace_id=context.workspace_id,
                task_id=context.task_id,
                operation=operation,
                scope_id=request.scope_id if request is not None else None,
                record_type=request.record_type.value if request is not None else None,
                outcome="success" if code is None else "error",
                error_code=code,
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
                policy_decision_id=decision_id,
            )
            try:
                self._audit.record(event)
            except Exception:
                raise ServiceNowAuditError("ServiceNow audit recording failed") from None
        if code is not None:
            return ServiceNowToolResult(
                operation=operation,
                outcome="error",
                error=ServiceNowToolError(
                    code=code,
                    message=_MESSAGES[code],
                    retryable=(
                        operation in (ServiceNowOperation.READ_RECORD, ServiceNowOperation.SEARCH)
                        and code
                        in {
                            ServiceNowErrorCode.UPSTREAM_TIMEOUT,
                            ServiceNowErrorCode.TRANSIENT_UPSTREAM_FAILURE,
                        }
                    ),
                ),
                correlation_id=context.resource_context.correlation_id,
                audit_ref=audit_ref,
            )
        return ServiceNowToolResult(
            operation=operation,
            outcome="success",
            data=data,
            correlation_id=context.resource_context.correlation_id,
            audit_ref=audit_ref,
        )

    @staticmethod
    def _scope_allowed(request: object, context: TrustedServiceNowContext) -> bool:
        profile = context.resource_context.profile.integrations.servicenow
        scopes = context.resource_context.authorization.allowed_scopes.servicenow_scopes
        if (
            not profile.enabled
            or request.scope_id not in profile.scopes
            or request.scope_id not in scopes
        ):
            return False
        if isinstance(request, CreateServiceNowRecordRequest) and profile.assignment_groups:
            # The initial create contract has no approved group selection/routing policy.
            return False
        group = getattr(request, "assignment_group", None)
        return group is None or not profile.assignment_groups or group in profile.assignment_groups

    def _dispatch(
        self, operation: ServiceNowOperation, context: ServiceNowProviderContext, request: object
    ) -> object:
        if operation is ServiceNowOperation.READ_RECORD and isinstance(
            request, GetServiceNowRecordRequest
        ):
            return self._provider.get_record(context, request)
        if operation is ServiceNowOperation.SEARCH and isinstance(
            request, SearchServiceNowRecordsRequest
        ):
            return self._provider.search_records(context, request)
        if operation is ServiceNowOperation.CREATE_RECORD and isinstance(
            request, CreateServiceNowRecordRequest
        ):
            return self._provider.create_record(context, request)
        if operation is ServiceNowOperation.UPDATE_RECORD and isinstance(
            request, UpdateServiceNowRecordRequest
        ):
            return self._provider.update_record(context, request)
        if operation is ServiceNowOperation.ADD_COMMENT and isinstance(
            request, AddServiceNowCommentRequest
        ):
            return self._provider.add_comment(context, request)
        raise TypeError("operation and request do not match")

    @staticmethod
    def _normalize(
        operation: ServiceNowOperation,
        request: object,
        raw: object,
        context: TrustedServiceNowContext,
    ) -> ServiceNowData:
        model_type = {
            ServiceNowOperation.READ_RECORD: ServiceNowRecordDetail,
            ServiceNowOperation.SEARCH: ServiceNowSearchPage,
            ServiceNowOperation.CREATE_RECORD: ServiceNowWriteResult,
            ServiceNowOperation.UPDATE_RECORD: ServiceNowWriteResult,
            ServiceNowOperation.ADD_COMMENT: ServiceNowJournalResult,
        }[operation]
        # Revalidate even a provider-supplied model_copy(update=...) instance; Pydantic
        # otherwise accepts an existing model without checking its mutated fields.
        data = model_type.model_validate(
            raw.model_dump() if isinstance(raw, BaseModel) else raw,
            from_attributes=True,
        )
        records = data.records if isinstance(data, ServiceNowSearchPage) else (data,)
        if isinstance(data, ServiceNowSearchPage) and len(data.records) > request.page_size:
            raise ValueError("provider exceeded page size")
        allowed_groups = context.resource_context.profile.integrations.servicenow.assignment_groups
        for item in records:
            if item.scope_id != request.scope_id or item.record_type != request.record_type:
                raise ValueError("provider result escaped logical scope/type")
            if (
                allowed_groups
                and isinstance(item, (ServiceNowRecordDetail,))
                and (item.assignment_group not in allowed_groups)
            ):
                raise ValueError("provider result escaped assignment group")
            if (
                allowed_groups
                and isinstance(data, ServiceNowSearchPage)
                and item.assignment_group not in allowed_groups
            ):
                raise ValueError("provider search result escaped assignment group")
            if isinstance(data, ServiceNowSearchPage) and (
                (request.number is not None and item.number != request.number)
                or (request.state is not None and item.state != request.state)
                or (
                    request.assignment_group is not None
                    and item.assignment_group != request.assignment_group
                )
            ):
                raise ValueError("provider search result ignored typed filter")
            if operation not in (ServiceNowOperation.CREATE_RECORD, ServiceNowOperation.SEARCH):
                if item.record_id != request.record_id:
                    raise ValueError("provider changed record identity")
        if isinstance(data, ServiceNowJournalResult) and data.channel != request.channel:
            raise ValueError("provider changed journal channel")
        return data
