"""Governed Jira entrypoint composed from existing policy, approval, and binding services."""

from collections.abc import Mapping
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from pydantic import ValidationError

from ai_dlc.application.approval import ApprovalNotFoundError, ApprovalService
from ai_dlc.application.authorization import JiraProjectTarget, ScopeRestriction
from ai_dlc.application.resource_bindings import (
    BindingNotFoundError,
    BindingResolutionDeniedError,
    JiraBinding,
    LogicalResourceRef,
    ResourceAccess,
    ResourceBindingRegistry,
    ResourceType,
)
from ai_dlc.application.tool_policy import (
    JiraOperation,
    ToolPolicyEffect,
    ToolPolicyReason,
    ToolPolicyRequest,
    ToolPolicyService,
)

from .models import (
    REQUEST_TYPES,
    AddJiraCommentRequest,
    CreateJiraIssueRequest,
    GetJiraIssueRequest,
    JiraCommentResult,
    JiraData,
    JiraErrorCode,
    JiraIssueDetail,
    JiraSearchPage,
    JiraToolError,
    JiraToolResult,
    JiraWriteResult,
    SearchJiraIssuesRequest,
    TransitionJiraIssueRequest,
    TrustedJiraContext,
    UpdateJiraIssueRequest,
)
from .ports import (
    JiraProvider,
    JiraProviderContext,
    JiraProviderFailure,
    JiraToolAuditEvent,
    JiraToolAuditSink,
)


class JiraAuditError(Exception):
    """An execution result cannot be returned when its audit event was not recorded."""


class JiraCancelled(Exception):
    """Trusted cancellation signal observed by an adapter."""


_MESSAGES = {
    JiraErrorCode.PERMISSION_DENIED: "Jira operation is not permitted",
    JiraErrorCode.INVALID_SCOPE: "Jira project is outside the selected initiative scope",
    JiraErrorCode.INVALID_ARGUMENT: "Invalid Jira operation arguments",
    JiraErrorCode.RESOURCE_NOT_FOUND: "Jira issue was not found",
    JiraErrorCode.UPSTREAM_AUTH_CONFIGURATION: "Jira connection is unavailable",
    JiraErrorCode.UPSTREAM_TIMEOUT: "Jira operation timed out",
    JiraErrorCode.MALFORMED_UPSTREAM_RESPONSE: "Jira returned an invalid response",
    JiraErrorCode.TRANSIENT_UPSTREAM_FAILURE: "Jira is temporarily unavailable",
    JiraErrorCode.CANCELLED: "Jira operation was cancelled",
}


class GovernedJiraService:
    def __init__(
        self,
        tool_policy: ToolPolicyService,
        bindings: ResourceBindingRegistry,
        provider: JiraProvider,
        audit_sink: JiraToolAuditSink,
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
        operation: JiraOperation,
        arguments: Mapping[str, object],
        *,
        context: TrustedJiraContext,
    ) -> JiraToolResult:
        """The MCP adapter supplies arguments; trusted context is injected separately."""
        if not isinstance(operation, JiraOperation) or not isinstance(context, TrustedJiraContext):
            raise TypeError("typed Jira operation and trusted context required")
        started = perf_counter()
        audit_ref = uuid4().hex
        request = None
        decision_id = None
        code: JiraErrorCode | None = None
        data: JiraData | None = None
        try:
            request_type = REQUEST_TYPES.get(operation)
            if request_type is None or not isinstance(arguments, Mapping):
                code = JiraErrorCode.INVALID_ARGUMENT
            else:
                try:
                    request = request_type.model_validate(arguments)
                except (ValidationError, ValueError, TypeError):
                    code = JiraErrorCode.INVALID_ARGUMENT
            if code is None and context.cancelled:
                code = JiraErrorCode.CANCELLED
            if code is None and request is not None:
                if not self._scope_allowed(request, context):
                    code = JiraErrorCode.INVALID_SCOPE
                else:
                    policy_request = ToolPolicyRequest(
                        context.resource_context.authorization.principal,
                        context.resource_context.authorization.initiative_id,
                        operation,
                        JiraProjectTarget(request.project_key),
                    )
                    restriction = ScopeRestriction(
                        jira_projects=context.resource_context.authorization.allowed_scopes.jira_projects
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
                                JiraErrorCode.INVALID_SCOPE
                                if decision.reason is ToolPolicyReason.TARGET_NOT_ALLOWED
                                else JiraErrorCode.PERMISSION_DENIED
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
                                code = JiraErrorCode.PERMISSION_DENIED
                    except ApprovalNotFoundError:
                        code = JiraErrorCode.PERMISSION_DENIED
                    except Exception:
                        # Policy/audit/approval infrastructure failure never allows execution.
                        code = JiraErrorCode.UPSTREAM_AUTH_CONFIGURATION
                    if code is None:
                        try:
                            resolved = self._bindings.resolve(
                                LogicalResourceRef(ResourceType.JIRA, "jira", request.project_key),
                                context=context.resource_context,
                                access=ResourceAccess.READ
                                if operation in (JiraOperation.READ_ISSUE, JiraOperation.SEARCH)
                                else ResourceAccess.WRITE,
                            )
                            binding = resolved.binding.details
                            if not isinstance(binding, JiraBinding):
                                raise ValueError("Jira binding has wrong type")
                            provider_context = JiraProviderContext(
                                binding,
                                context.resource_context.correlation_id,
                                context.timeout_seconds,
                            )
                            raw = self._dispatch(operation, provider_context, request)
                            data = self._normalize(operation, request, raw)
                        except (BindingNotFoundError, BindingResolutionDeniedError):
                            code = JiraErrorCode.UPSTREAM_AUTH_CONFIGURATION
                        except JiraProviderFailure as exc:
                            code = exc.code
                        except TimeoutError:
                            code = JiraErrorCode.UPSTREAM_TIMEOUT
                        except JiraCancelled:
                            code = JiraErrorCode.CANCELLED
                        except (ValidationError, ValueError, TypeError):
                            code = JiraErrorCode.MALFORMED_UPSTREAM_RESPONSE
                        except Exception:
                            code = JiraErrorCode.TRANSIENT_UPSTREAM_FAILURE
        except Exception:
            # A failure in trusted orchestration also fails closed and is audited as an error.
            code = JiraErrorCode.UPSTREAM_AUTH_CONFIGURATION
        finally:
            event = JiraToolAuditEvent(
                audit_ref=audit_ref,
                occurred_at=datetime.now(UTC),
                correlation_id=context.resource_context.correlation_id,
                principal_id=context.resource_context.authorization.principal.subject_id,
                initiative_id=context.resource_context.authorization.initiative_id,
                workspace_id=context.workspace_id,
                task_id=context.task_id,
                operation=operation,
                project_key=request.project_key if request is not None else None,
                outcome="success" if code is None else "error",
                error_code=code,
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
                policy_decision_id=decision_id,
            )
            try:
                self._audit.record(event)
            except Exception:
                raise JiraAuditError("Jira audit recording failed") from None
        if code is not None:
            return JiraToolResult(
                operation=operation,
                outcome="error",
                error=JiraToolError(
                    code=code,
                    message=_MESSAGES[code],
                    retryable=(
                        operation in (JiraOperation.READ_ISSUE, JiraOperation.SEARCH)
                        and code
                        in {
                            JiraErrorCode.UPSTREAM_TIMEOUT,
                            JiraErrorCode.TRANSIENT_UPSTREAM_FAILURE,
                        }
                    ),
                ),
                correlation_id=context.resource_context.correlation_id,
                audit_ref=audit_ref,
            )
        return JiraToolResult(
            operation=operation,
            outcome="success",
            data=data,
            correlation_id=context.resource_context.correlation_id,
            audit_ref=audit_ref,
        )

    @staticmethod
    def _scope_allowed(request: object, context: TrustedJiraContext) -> bool:
        project = request.project_key
        profile = context.resource_context.profile
        allowed = context.resource_context.authorization.allowed_scopes.jira_projects
        if (
            not profile.integrations.jira.enabled
            or project not in profile.integrations.jira.projects
            or project not in allowed
        ):
            return False
        issue_key = getattr(request, "issue_key", None)
        return issue_key is None or issue_key.rsplit("-", 1)[0] == project

    def _dispatch(
        self, operation: JiraOperation, context: JiraProviderContext, request: object
    ) -> object:
        if operation is JiraOperation.READ_ISSUE and isinstance(request, GetJiraIssueRequest):
            return self._provider.get_issue(context, request)
        if operation is JiraOperation.SEARCH and isinstance(request, SearchJiraIssuesRequest):
            return self._provider.search_issues(context, request)
        if operation is JiraOperation.CREATE_ISSUE and isinstance(request, CreateJiraIssueRequest):
            return self._provider.create_issue(context, request)
        if operation is JiraOperation.UPDATE_ISSUE and isinstance(request, UpdateJiraIssueRequest):
            return self._provider.update_issue(context, request)
        if operation is JiraOperation.TRANSITION_ISSUE and isinstance(
            request, TransitionJiraIssueRequest
        ):
            return self._provider.transition_issue(context, request)
        if operation is JiraOperation.ADD_COMMENT and isinstance(request, AddJiraCommentRequest):
            return self._provider.add_comment(context, request)
        raise TypeError("operation and request do not match")

    @staticmethod
    def _normalize(operation: JiraOperation, request: object, raw: object) -> JiraData:
        model_type = {
            JiraOperation.READ_ISSUE: JiraIssueDetail,
            JiraOperation.SEARCH: JiraSearchPage,
            JiraOperation.CREATE_ISSUE: JiraWriteResult,
            JiraOperation.UPDATE_ISSUE: JiraWriteResult,
            JiraOperation.TRANSITION_ISSUE: JiraWriteResult,
            JiraOperation.ADD_COMMENT: JiraCommentResult,
        }[operation]
        data = model_type.model_validate(raw, from_attributes=True)
        if isinstance(data, JiraSearchPage):
            if len(data.issues) > request.page_size or any(
                item.project_key != request.project_key for item in data.issues
            ):
                raise ValueError("search result escaped project scope")
        else:
            if data.project_key != request.project_key:
                raise ValueError("provider result escaped project scope")
            if (
                operation not in (JiraOperation.CREATE_ISSUE, JiraOperation.SEARCH)
                and data.issue_key != request.issue_key
            ):
                raise ValueError("provider result changed issue identity")
        return data
