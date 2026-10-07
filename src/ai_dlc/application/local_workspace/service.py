"""Governed task-scoped local execution, separate from remote Git providers."""

import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from threading import Lock
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
    GitTarget,
    ToolPolicyEffect,
    ToolPolicyReason,
    ToolPolicyRequest,
    ToolPolicyService,
    operation_risk,
)
from ai_dlc.application.tool_policy.operations import ToolOperationRisk

from .models import (
    POLICY_OPERATIONS,
    REQUEST_TYPES,
    ApplyPatchRequest,
    BuildWorkspaceRequest,
    CheckoutWorkspaceRequest,
    CleanupWorkspaceRequest,
    CommitResult,
    CommitWorkspaceRequest,
    DiffWorkspaceRequest,
    LocalDiff,
    LocalStatus,
    LocalWorkspaceOperation,
    PatchResult,
    PrepareWorkspaceRequest,
    ProcessResult,
    StatusWorkspaceRequest,
    TestWorkspaceRequest,
    TrustedCommitHandoff,
    TrustedWorkspaceContext,
    WorkspaceData,
    WorkspaceErrorCode,
    WorkspaceInfo,
    WorkspaceKey,
    WorkspaceState,
    WorkspaceToolError,
    WorkspaceToolResult,
    valid_relative_path,
)
from .ports import (
    CommitHandoffStore,
    WorkspaceAuditEvent,
    WorkspaceAuditSink,
    WorkspaceExecutor,
    WorkspaceFailure,
    WorkspaceManager,
)


class WorkspaceAuditError(Exception):
    """An execution result cannot be returned without its audit record."""


_MESSAGES = {
    WorkspaceErrorCode.PERMISSION_DENIED: "Local workspace operation is not permitted",
    WorkspaceErrorCode.INVALID_SCOPE: "Repository is outside the selected initiative scope",
    WorkspaceErrorCode.INVALID_ARGUMENT: "Invalid local workspace arguments",
    WorkspaceErrorCode.WORKSPACE_NOT_FOUND: "Task workspace was not found",
    WorkspaceErrorCode.WORKSPACE_NOT_ACTIVE: "Task workspace is not active",
    WorkspaceErrorCode.EXECUTION_TIMEOUT: "Local operation timed out",
    WorkspaceErrorCode.EXECUTION_FAILED: "Local operation failed",
    WorkspaceErrorCode.PATCH_REJECTED: "Patch was rejected",
    WorkspaceErrorCode.RESOURCE_NOT_FOUND: "Repository source was not found",
    WorkspaceErrorCode.CANCELLED: "Local operation was cancelled",
    WorkspaceErrorCode.RUNTIME_CONFIGURATION: "Local execution is unavailable",
}


def configured_argv(command: str) -> tuple[str, ...]:
    """Parse trusted Profile text into argv and reject shell syntax; never shell=True."""
    if any(char in command for char in ("\n", "\r", "`", "$", "|", ";", "<", ">", "&")):
        raise WorkspaceFailure("RUNTIME_CONFIGURATION")
    try:
        argv = tuple(shlex.split(command, posix=True))
    except ValueError:
        raise WorkspaceFailure("RUNTIME_CONFIGURATION") from None
    if (
        not argv
        or any("\0" in token for token in argv)
        or "/" in argv[0]
        or argv[0] in {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh", "env"}
        or any(token in {"-c", "-e", "--eval", "--execute"} for token in argv[1:])
        or len(argv) > 32
        or len(command.encode("utf-8")) > 4096
    ):
        raise WorkspaceFailure("RUNTIME_CONFIGURATION")
    return argv


class GovernedWorkspaceService:
    def __init__(
        self,
        tool_policy: ToolPolicyService,
        bindings: ResourceBindingRegistry,
        manager: WorkspaceManager,
        executor: WorkspaceExecutor,
        handoffs: CommitHandoffStore,
        audit_sink: WorkspaceAuditSink,
        *,
        approvals: ApprovalService | None = None,
    ) -> None:
        self._tool_policy = tool_policy
        self._bindings = bindings
        self._manager = manager
        self._executor = executor
        self._handoffs = handoffs
        self._audit = audit_sink
        self._approvals = approvals
        self._locks_guard = Lock()
        self._locks: dict[WorkspaceKey, Lock] = {}

    def invoke(
        self,
        operation: LocalWorkspaceOperation,
        arguments: Mapping[str, object],
        *,
        context: TrustedWorkspaceContext,
    ) -> WorkspaceToolResult:
        if not isinstance(operation, LocalWorkspaceOperation) or not isinstance(
            context, TrustedWorkspaceContext
        ):
            raise TypeError("typed local operation and trusted context required")
        started = perf_counter()
        audit_ref = uuid4().hex
        request = None
        decision_id = None
        code: WorkspaceErrorCode | None = None
        data: WorkspaceData | None = None
        try:
            request_type = REQUEST_TYPES.get(operation)
            if request_type is None or not isinstance(arguments, Mapping):
                code = WorkspaceErrorCode.INVALID_ARGUMENT
            else:
                try:
                    request = request_type.model_validate(arguments)
                except (ValidationError, TypeError, ValueError):
                    code = WorkspaceErrorCode.INVALID_ARGUMENT
            if code is None and context.cancelled:
                code = WorkspaceErrorCode.CANCELLED
            if code is None and request is not None:
                repo = self._profile_repository(request.repository_id, context)
                if repo is None:
                    code = WorkspaceErrorCode.INVALID_SCOPE
                else:
                    try:
                        key = WorkspaceKey(
                            context.resource_context.authorization.initiative_id,
                            context.workspace_id,
                            context.task_id,
                            request.repository_id,
                        )
                        with self._lock_for(key):
                            data, decision_id = self._governed_execute(
                                operation, request, repo.default_branch, context
                            )
                    except ApprovalNotFoundError:
                        code = WorkspaceErrorCode.PERMISSION_DENIED
                    except (BindingNotFoundError, BindingResolutionDeniedError):
                        code = WorkspaceErrorCode.RUNTIME_CONFIGURATION
                    except WorkspaceFailure as exc:
                        decision_id = exc.decision_id
                        try:
                            code = WorkspaceErrorCode(exc.code)
                        except ValueError:
                            code = WorkspaceErrorCode.EXECUTION_FAILED
                    except (ValidationError, ValueError, TypeError):
                        code = WorkspaceErrorCode.EXECUTION_FAILED
                    except Exception:
                        code = WorkspaceErrorCode.RUNTIME_CONFIGURATION
        except Exception:
            code = WorkspaceErrorCode.RUNTIME_CONFIGURATION
        finally:
            event = WorkspaceAuditEvent(
                audit_ref=audit_ref,
                occurred_at=datetime.now(UTC),
                correlation_id=context.resource_context.correlation_id,
                principal_id=context.resource_context.authorization.principal.subject_id,
                initiative_id=context.resource_context.authorization.initiative_id,
                workspace_id=context.workspace_id,
                task_id=context.task_id,
                repository_id=request.repository_id if request is not None else None,
                operation=operation.value,
                branch=request.branch if isinstance(request, CheckoutWorkspaceRequest) else None,
                outcome="success" if code is None else "error",
                error_code=code.value if code is not None else None,
                latency_ms=max(0, int((perf_counter() - started) * 1000)),
                policy_decision_id=decision_id,
                commit_sha=data.commit_sha if isinstance(data, CommitResult) else None,
            )
            try:
                self._audit.record(event)
            except Exception:
                raise WorkspaceAuditError("Workspace audit recording failed") from None
        if code is None and isinstance(data, CommitResult):
            try:
                self._handoffs.record(
                    TrustedCommitHandoff(
                        context.resource_context.authorization.initiative_id,
                        context.workspace_id,
                        context.task_id,
                        request.repository_id,
                        context.resource_context.authorization.principal.subject_id,
                        data.commit_sha,
                        datetime.now(UTC),
                    )
                )
            except Exception:
                # The local commit exists, but no remote update is approved.
                code = WorkspaceErrorCode.RUNTIME_CONFIGURATION
        if code is not None:
            return WorkspaceToolResult(
                operation=operation,
                logical_resource_id=request.repository_id if request is not None else "unknown",
                outcome="error",
                error=WorkspaceToolError(code=code, message=_MESSAGES[code]),
                correlation_id=context.resource_context.correlation_id,
                audit_ref=audit_ref,
            )
        return WorkspaceToolResult(
            operation=operation,
            logical_resource_id=request.repository_id,
            outcome="success",
            data=data,
            correlation_id=context.resource_context.correlation_id,
            audit_ref=audit_ref,
        )

    def _lock_for(self, key: WorkspaceKey) -> Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, Lock())

    @staticmethod
    def _profile_repository(repository_id: str, context: TrustedWorkspaceContext):
        profile = context.resource_context.profile.integrations.git
        scopes = context.resource_context.authorization.allowed_scopes.repository_ids
        if not profile.enabled or repository_id not in scopes:
            return None
        return next((item for item in profile.repositories if item.id == repository_id), None)

    def _governed_execute(self, operation, request, default_branch, context):
        principal_id = context.resource_context.authorization.principal.subject_id
        key = WorkspaceKey(
            context.resource_context.authorization.initiative_id,
            context.workspace_id,
            context.task_id,
            request.repository_id,
        )
        record = self._manager.get(key, principal_id)
        if operation is not LocalWorkspaceOperation.PREPARE and record is None:
            raise WorkspaceFailure("WORKSPACE_NOT_FOUND")
        if (
            record is not None
            and record.state is not WorkspaceState.ACTIVE
            and (operation is not LocalWorkspaceOperation.CLEANUP)
        ):
            raise WorkspaceFailure("WORKSPACE_NOT_ACTIVE")
        branch = (
            request.branch
            if isinstance(request, CheckoutWorkspaceRequest)
            else record.branch
            if record is not None
            else default_branch
        )
        policy_operation = POLICY_OPERATIONS[operation]
        policy_request = ToolPolicyRequest(
            context.resource_context.authorization.principal,
            context.resource_context.authorization.initiative_id,
            policy_operation,
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
        except Exception:
            raise WorkspaceFailure("RUNTIME_CONFIGURATION") from None
        if decision.effect is ToolPolicyEffect.DENY:
            raise WorkspaceFailure(
                "INVALID_SCOPE"
                if decision.reason is ToolPolicyReason.TARGET_NOT_ALLOWED
                else "PERMISSION_DENIED",
                decision.decision_id,
            )
        if decision.effect is ToolPolicyEffect.REQUIRE_APPROVAL and (
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
            raise WorkspaceFailure("PERMISSION_DENIED", decision.decision_id)
        binding = None
        if operation is not LocalWorkspaceOperation.CLEANUP:
            resolved = self._bindings.resolve(
                LogicalResourceRef(ResourceType.GIT_REPOSITORY, request.repository_id),
                context=context.resource_context,
                access=ResourceAccess.READ
                if operation_risk(policy_operation) is ToolOperationRisk.READ
                else ResourceAccess.WRITE,
            )
            binding = resolved.binding.details
            if not isinstance(binding, GitRepositoryBinding):
                raise WorkspaceFailure("RUNTIME_CONFIGURATION")
        raw = self._execute(
            operation, request, context, key, record, binding, default_branch, principal_id
        )
        data = self._normalize(operation, request.repository_id, raw)
        return data, decision.decision_id

    def _execute(
        self,
        operation,
        request,
        context,
        key,
        record,
        binding: GitRepositoryBinding | None,
        default_branch: str,
        principal_id: str,
    ) -> object:
        if operation is LocalWorkspaceOperation.PREPARE and isinstance(
            request, PrepareWorkspaceRequest
        ):
            record = self._manager.prepare(
                key, principal_id, binding, default_branch, context.timeout_seconds
            )
            return WorkspaceInfo(
                repository_id=request.repository_id, state=record.state, branch=record.branch
            )
        if operation is LocalWorkspaceOperation.CLEANUP and isinstance(
            request, CleanupWorkspaceRequest
        ):
            record = self._manager.cleanup(key, principal_id)
            return WorkspaceInfo(
                repository_id=request.repository_id, state=record.state, branch=record.branch
            )
        if operation is LocalWorkspaceOperation.CHECKOUT and isinstance(
            request, CheckoutWorkspaceRequest
        ):
            self._executor.checkout(record, request.branch, context.timeout_seconds)
            record = self._manager.set_branch(key, principal_id, request.branch)
            return WorkspaceInfo(
                repository_id=request.repository_id, state=record.state, branch=record.branch
            )
        if operation is LocalWorkspaceOperation.STATUS and isinstance(
            request, StatusWorkspaceRequest
        ):
            return self._executor.status(record, context.timeout_seconds)
        if operation is LocalWorkspaceOperation.DIFF and isinstance(request, DiffWorkspaceRequest):
            return self._executor.diff(record, context.timeout_seconds)
        if operation is LocalWorkspaceOperation.APPLY_PATCH and isinstance(
            request, ApplyPatchRequest
        ):
            return self._executor.apply_patch(record, request.patch, context.timeout_seconds)
        if operation in (
            LocalWorkspaceOperation.BUILD,
            LocalWorkspaceOperation.TEST,
        ) and isinstance(request, (BuildWorkspaceRequest, TestWorkspaceRequest)):
            build = next(
                (
                    item
                    for item in context.resource_context.profile.build_profiles
                    if item.id == request.build_profile_id
                    and item.repository_id in (None, request.repository_id)
                ),
                None,
            )
            if build is None:
                raise WorkspaceFailure("INVALID_SCOPE")
            command = (
                build.build_command
                if operation is LocalWorkspaceOperation.BUILD
                else build.test_command
            )
            if command is None:
                raise WorkspaceFailure("RUNTIME_CONFIGURATION")
            return self._executor.run_configured(
                record,
                operation.value,
                configured_argv(command),
                build.working_directory,
                context.timeout_seconds,
            )
        if operation is LocalWorkspaceOperation.COMMIT and isinstance(
            request, CommitWorkspaceRequest
        ):
            return self._executor.commit(
                record, request.message, request.paths, context.timeout_seconds
            )
        raise WorkspaceFailure("INVALID_ARGUMENT")

    @staticmethod
    def _normalize(
        operation: LocalWorkspaceOperation, repository_id: str, raw: object
    ) -> WorkspaceData:
        model = {
            LocalWorkspaceOperation.PREPARE: WorkspaceInfo,
            LocalWorkspaceOperation.CLEANUP: WorkspaceInfo,
            LocalWorkspaceOperation.CHECKOUT: WorkspaceInfo,
            LocalWorkspaceOperation.STATUS: LocalStatus,
            LocalWorkspaceOperation.DIFF: LocalDiff,
            LocalWorkspaceOperation.APPLY_PATCH: PatchResult,
            LocalWorkspaceOperation.BUILD: ProcessResult,
            LocalWorkspaceOperation.TEST: ProcessResult,
            LocalWorkspaceOperation.COMMIT: CommitResult,
        }[operation]
        data = model.model_validate(
            raw.model_dump() if isinstance(raw, BaseModel) else raw, from_attributes=True
        )
        if data.repository_id != repository_id:
            raise ValueError("executor changed logical repository")
        if isinstance(data, LocalStatus) and any(
            not valid_relative_path(path)
            for path in (*data.staged_files, *data.modified_files, *data.untracked_files)
        ):
            raise ValueError("executor returned invalid status path")
        if isinstance(data, (LocalDiff, PatchResult)) and any(
            not valid_relative_path(path) for path in data.changed_files
        ):
            raise ValueError("executor returned invalid changed path")
        if isinstance(data, LocalDiff) and len(data.patch.encode("utf-8")) > 32768:
            raise ValueError("executor returned unbounded diff")
        return data
