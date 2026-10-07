"""Governed remote Git execution; local workspace operations belong to AIDLC-35."""

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
    GitRepositoryBinding,
    LogicalResourceRef,
    ResourceAccess,
    ResourceBindingRegistry,
    ResourceType,
)
from ai_dlc.application.tool_policy import (
    GitOperation,
    GitTarget,
    ToolPolicyEffect,
    ToolPolicyReason,
    ToolPolicyRequest,
    ToolPolicyService,
)
from ai_dlc.domain.initiative.enums import RepositoryAccess

from .models import (
    REQUEST_TYPES,
    BranchInfo,
    BranchPage,
    CreateBranchRequest,
    CreatePullRequestRequest,
    GetBranchRequest,
    GetPullRequestRequest,
    GetRemoteDiffRequest,
    GetRepositoryRequest,
    GitData,
    GitErrorCode,
    GitToolError,
    GitToolResult,
    ListBranchesRequest,
    PullRequestInfo,
    RemoteDiff,
    RepositoryInfo,
    TrustedGitContext,
    UpdateBranchRequest,
    UpdatePullRequestRequest,
)
from .ports import (
    GitProviderContext,
    GitProviderFailure,
    GitToolAuditEvent,
    GitToolAuditSink,
    RemoteGitProvider,
    RemoteGitProviderRegistry,
)


class GitAuditError(Exception):
    """An execution result cannot be returned without its audit record."""


class GitCancelled(Exception):
    """Trusted cancellation observed by an adapter."""


_READS = frozenset(
    {
        GitOperation.READ_REPOSITORY,
        GitOperation.READ_BRANCH,
        GitOperation.LIST_BRANCHES,
        GitOperation.READ_DIFF,
        GitOperation.READ_PR,
    }
)
_MESSAGES = {
    GitErrorCode.PERMISSION_DENIED: "Remote Git operation is not permitted",
    GitErrorCode.INVALID_SCOPE: "Repository is outside the selected initiative scope",
    GitErrorCode.INVALID_ARGUMENT: "Invalid remote Git arguments or conflicting remote state",
    GitErrorCode.RESOURCE_NOT_FOUND: "Remote Git resource was not found",
    GitErrorCode.UPSTREAM_AUTH_CONFIGURATION: "Remote Git connection is unavailable",
    GitErrorCode.UPSTREAM_TIMEOUT: "Remote Git operation timed out",
    GitErrorCode.MALFORMED_UPSTREAM_RESPONSE: "Remote Git returned an invalid response",
    GitErrorCode.TRANSIENT_UPSTREAM_FAILURE: "Remote Git is temporarily unavailable",
    GitErrorCode.CANCELLED: "Remote Git operation was cancelled",
}


class GovernedRemoteGitService:
    def __init__(
        self,
        tool_policy: ToolPolicyService,
        bindings: ResourceBindingRegistry,
        providers: RemoteGitProviderRegistry,
        audit_sink: GitToolAuditSink,
        *,
        approvals: ApprovalService | None = None,
    ) -> None:
        self._tool_policy = tool_policy
        self._bindings = bindings
        self._providers = providers
        self._audit = audit_sink
        self._approvals = approvals

    def invoke(
        self,
        operation: GitOperation,
        arguments: Mapping[str, object],
        *,
        context: TrustedGitContext,
    ) -> GitToolResult:
        if not isinstance(operation, GitOperation) or not isinstance(context, TrustedGitContext):
            raise TypeError("typed Git operation and trusted context required")
        started = perf_counter()
        audit_ref = uuid4().hex
        request = None
        decision_id = None
        code: GitErrorCode | None = None
        data: GitData | None = None
        try:
            request_type = REQUEST_TYPES.get(operation)
            if request_type is None or not isinstance(arguments, Mapping):
                code = GitErrorCode.INVALID_ARGUMENT
            else:
                try:
                    request = request_type.model_validate(arguments)
                except (ValidationError, ValueError, TypeError):
                    code = GitErrorCode.INVALID_ARGUMENT
            if code is None and context.cancelled:
                code = GitErrorCode.CANCELLED
            if code is None and request is not None:
                repository = self._profile_repository(request.repository_id, context)
                if repository is None:
                    code = GitErrorCode.INVALID_SCOPE
                elif (
                    operation not in _READS and repository.access is not RepositoryAccess.READ_WRITE
                ):
                    code = GitErrorCode.PERMISSION_DENIED
                elif isinstance(request, UpdateBranchRequest) and (
                    (request.repository_id, request.commit_sha) not in context.approved_commits
                ):
                    code = GitErrorCode.PERMISSION_DENIED
                else:
                    branch = self._policy_branch(request)
                    policy_request = ToolPolicyRequest(
                        context.resource_context.authorization.principal,
                        context.resource_context.authorization.initiative_id,
                        operation,
                        GitTarget(request.repository_id, branch),
                    )
                    restriction = ScopeRestriction(
                        repository_ids=context.resource_context.authorization.allowed_scopes.repository_ids
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
                                GitErrorCode.INVALID_SCOPE
                                if decision.reason is ToolPolicyReason.TARGET_NOT_ALLOWED
                                else GitErrorCode.PERMISSION_DENIED
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
                                code = GitErrorCode.PERMISSION_DENIED
                    except ApprovalNotFoundError:
                        code = GitErrorCode.PERMISSION_DENIED
                    except Exception:
                        code = GitErrorCode.UPSTREAM_AUTH_CONFIGURATION
                    if code is None:
                        try:
                            resolved = self._bindings.resolve(
                                LogicalResourceRef(
                                    ResourceType.GIT_REPOSITORY, request.repository_id
                                ),
                                context=context.resource_context,
                                access=ResourceAccess.READ
                                if operation in _READS
                                else ResourceAccess.WRITE,
                            )
                            binding = resolved.binding.details
                            if not isinstance(binding, GitRepositoryBinding):
                                raise ValueError("Git binding has wrong type")
                            provider = self._providers.get(binding.provider)
                            if provider is None:
                                code = GitErrorCode.UPSTREAM_AUTH_CONFIGURATION
                            else:
                                provider_context = GitProviderContext(
                                    binding,
                                    context.resource_context.correlation_id,
                                    context.timeout_seconds,
                                )
                                if isinstance(request, UpdatePullRequestRequest):
                                    current = provider.get_pull_request(
                                        provider_context,
                                        GetPullRequestRequest(
                                            repository_id=request.repository_id,
                                            number=request.number,
                                        ),
                                    )
                                    current = self._normalize(
                                        GitOperation.READ_PR,
                                        GetPullRequestRequest(
                                            repository_id=request.repository_id,
                                            number=request.number,
                                        ),
                                        current,
                                        repository.default_branch,
                                    )
                                    if current.head_branch != request.head_branch:
                                        raise ValueError("PR head differs from approved target")
                                raw = self._dispatch(provider, operation, provider_context, request)
                                data = self._normalize(
                                    operation, request, raw, repository.default_branch
                                )
                        except (BindingNotFoundError, BindingResolutionDeniedError):
                            code = GitErrorCode.UPSTREAM_AUTH_CONFIGURATION
                        except GitProviderFailure as exc:
                            code = exc.code
                        except TimeoutError:
                            code = GitErrorCode.UPSTREAM_TIMEOUT
                        except GitCancelled:
                            code = GitErrorCode.CANCELLED
                        except (ValidationError, ValueError, TypeError):
                            code = GitErrorCode.MALFORMED_UPSTREAM_RESPONSE
                        except Exception:
                            code = GitErrorCode.TRANSIENT_UPSTREAM_FAILURE
        except Exception:
            code = GitErrorCode.UPSTREAM_AUTH_CONFIGURATION
        finally:
            event = GitToolAuditEvent(
                audit_ref=audit_ref,
                occurred_at=datetime.now(UTC),
                correlation_id=context.resource_context.correlation_id,
                principal_id=context.resource_context.authorization.principal.subject_id,
                initiative_id=context.resource_context.authorization.initiative_id,
                workspace_id=context.workspace_id,
                task_id=context.task_id,
                operation=operation,
                repository_id=request.repository_id if request is not None else None,
                branch=self._policy_branch(request) if request is not None else None,
                pull_request_number=getattr(request, "number", None),
                outcome="success" if code is None else "error",
                error_code=code,
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
                policy_decision_id=decision_id,
            )
            try:
                self._audit.record(event)
            except Exception:
                raise GitAuditError("Remote Git audit recording failed") from None
        if code is not None:
            return GitToolResult(
                operation=operation,
                logical_resource_id=request.repository_id if request is not None else "unknown",
                outcome="error",
                error=GitToolError(
                    code=code,
                    message=_MESSAGES[code],
                    retryable=operation in _READS
                    and code
                    in {GitErrorCode.UPSTREAM_TIMEOUT, GitErrorCode.TRANSIENT_UPSTREAM_FAILURE},
                ),
                correlation_id=context.resource_context.correlation_id,
                audit_ref=audit_ref,
            )
        return GitToolResult(
            operation=operation,
            logical_resource_id=request.repository_id,
            outcome="success",
            data=data,
            correlation_id=context.resource_context.correlation_id,
            audit_ref=audit_ref,
        )

    @staticmethod
    def _profile_repository(repository_id: str, context: TrustedGitContext):
        profile = context.resource_context.profile.integrations.git
        allowed = context.resource_context.authorization.allowed_scopes.repository_ids
        if not profile.enabled or repository_id not in allowed:
            return None
        return next((item for item in profile.repositories if item.id == repository_id), None)

    @staticmethod
    def _policy_branch(request: object) -> str | None:
        return getattr(request, "branch", None) or getattr(request, "head_branch", None)

    @staticmethod
    def _dispatch(
        provider: RemoteGitProvider,
        operation: GitOperation,
        context: GitProviderContext,
        request: object,
    ) -> object:
        if operation is GitOperation.READ_REPOSITORY and isinstance(request, GetRepositoryRequest):
            return provider.get_repository(context, request)
        if operation is GitOperation.READ_BRANCH and isinstance(request, GetBranchRequest):
            return provider.get_branch(context, request)
        if operation is GitOperation.LIST_BRANCHES and isinstance(request, ListBranchesRequest):
            return provider.list_branches(context, request)
        if operation is GitOperation.READ_DIFF and isinstance(request, GetRemoteDiffRequest):
            return provider.get_diff(context, request)
        if operation is GitOperation.READ_PR and isinstance(request, GetPullRequestRequest):
            return provider.get_pull_request(context, request)
        if operation is GitOperation.CREATE_BRANCH and isinstance(request, CreateBranchRequest):
            return provider.create_branch(context, request)
        if operation is GitOperation.PUSH and isinstance(request, UpdateBranchRequest):
            return provider.update_branch(context, request)
        if operation is GitOperation.CREATE_PR and isinstance(request, CreatePullRequestRequest):
            return provider.create_pull_request(context, request)
        if operation is GitOperation.UPDATE_PR and isinstance(request, UpdatePullRequestRequest):
            return provider.update_pull_request(context, request)
        raise TypeError("operation and request do not match")

    @staticmethod
    def _normalize(
        operation: GitOperation, request: object, raw: object, default_branch: str
    ) -> GitData:
        model_type = {
            GitOperation.READ_REPOSITORY: RepositoryInfo,
            GitOperation.READ_BRANCH: BranchInfo,
            GitOperation.LIST_BRANCHES: BranchPage,
            GitOperation.READ_DIFF: RemoteDiff,
            GitOperation.READ_PR: PullRequestInfo,
            GitOperation.CREATE_BRANCH: BranchInfo,
            GitOperation.PUSH: BranchInfo,
            GitOperation.CREATE_PR: PullRequestInfo,
            GitOperation.UPDATE_PR: PullRequestInfo,
        }[operation]
        data = model_type.model_validate(
            raw.model_dump() if isinstance(raw, BaseModel) else raw, from_attributes=True
        )
        if data.repository_id != request.repository_id:
            raise ValueError("provider result changed repository")
        if isinstance(data, RepositoryInfo) and data.default_branch != default_branch:
            raise ValueError("provider default branch differs from Profile")
        if isinstance(data, BranchInfo):
            if data.name != request.branch:
                raise ValueError("provider changed branch identity")
            if isinstance(request, UpdateBranchRequest) and data.head_sha != request.commit_sha:
                raise ValueError("provider did not update requested SHA")
        if isinstance(data, BranchPage):
            if len(data.branches) > request.page_size or any(
                item.repository_id != request.repository_id for item in data.branches
            ):
                raise ValueError("provider branch page escaped scope")
        if isinstance(data, RemoteDiff) and (
            data.base_ref != request.base_ref or data.head_ref != request.head_ref
        ):
            raise ValueError("provider changed diff refs")
        if isinstance(data, PullRequestInfo):
            if isinstance(request, (GetPullRequestRequest, UpdatePullRequestRequest)) and (
                data.number != request.number
            ):
                raise ValueError("provider changed PR number")
            if isinstance(request, CreatePullRequestRequest) and (
                data.base_branch != request.base_branch or data.head_branch != request.head_branch
            ):
                raise ValueError("provider changed PR refs")
            if (
                isinstance(request, UpdatePullRequestRequest)
                and data.head_branch != request.head_branch
            ):
                raise ValueError("provider changed PR head")
        return data
