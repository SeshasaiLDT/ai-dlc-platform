"""Stable tool operations with deterministic base permissions and risk."""

from enum import StrEnum

from ai_dlc.domain.identity import ToolPermission


class ToolKind(StrEnum):
    JIRA = "jira"
    GIT = "git"
    SERVICENOW = "servicenow"


class ToolOperationRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class JiraOperation(StrEnum):
    SEARCH = "search"
    READ_ISSUE = "read_issue"
    ADD_COMMENT = "add_comment"
    UPDATE_ISSUE = "update_issue"
    TRANSITION_ISSUE = "transition_issue"
    CREATE_ISSUE = "create_issue"
    DELETE_ISSUE = "delete_issue"


class GitOperation(StrEnum):
    READ_REPOSITORY = "read_repository"
    READ_BRANCH = "read_branch"
    READ_DIFF = "read_diff"
    CREATE_BRANCH = "create_branch"
    COMMIT = "commit"
    PUSH = "push"
    CREATE_PR = "create_pr"
    UPDATE_PR = "update_pr"
    DELETE_BRANCH = "delete_branch"


class ServiceNowOperation(StrEnum):
    SEARCH = "search"
    READ_RECORD = "read_record"
    ADD_COMMENT = "add_comment"
    UPDATE_RECORD = "update_record"
    CREATE_RECORD = "create_record"
    DELETE_RECORD = "delete_record"


ToolOperation = JiraOperation | GitOperation | ServiceNowOperation

_RISK: dict[tuple[ToolKind, str], ToolOperationRisk] = {
    (ToolKind.JIRA, JiraOperation.SEARCH): ToolOperationRisk.READ,
    (ToolKind.JIRA, JiraOperation.READ_ISSUE): ToolOperationRisk.READ,
    (ToolKind.JIRA, JiraOperation.ADD_COMMENT): ToolOperationRisk.WRITE,
    (ToolKind.JIRA, JiraOperation.UPDATE_ISSUE): ToolOperationRisk.WRITE,
    (ToolKind.JIRA, JiraOperation.TRANSITION_ISSUE): ToolOperationRisk.WRITE,
    (ToolKind.JIRA, JiraOperation.CREATE_ISSUE): ToolOperationRisk.WRITE,
    (ToolKind.JIRA, JiraOperation.DELETE_ISSUE): ToolOperationRisk.DESTRUCTIVE,
    (ToolKind.GIT, GitOperation.READ_REPOSITORY): ToolOperationRisk.READ,
    (ToolKind.GIT, GitOperation.READ_BRANCH): ToolOperationRisk.READ,
    (ToolKind.GIT, GitOperation.READ_DIFF): ToolOperationRisk.READ,
    (ToolKind.GIT, GitOperation.CREATE_BRANCH): ToolOperationRisk.WRITE,
    (ToolKind.GIT, GitOperation.COMMIT): ToolOperationRisk.WRITE,
    (ToolKind.GIT, GitOperation.PUSH): ToolOperationRisk.WRITE,
    (ToolKind.GIT, GitOperation.CREATE_PR): ToolOperationRisk.WRITE,
    (ToolKind.GIT, GitOperation.UPDATE_PR): ToolOperationRisk.WRITE,
    (ToolKind.GIT, GitOperation.DELETE_BRANCH): ToolOperationRisk.DESTRUCTIVE,
    (ToolKind.SERVICENOW, ServiceNowOperation.SEARCH): ToolOperationRisk.READ,
    (ToolKind.SERVICENOW, ServiceNowOperation.READ_RECORD): ToolOperationRisk.READ,
    (ToolKind.SERVICENOW, ServiceNowOperation.ADD_COMMENT): ToolOperationRisk.WRITE,
    (ToolKind.SERVICENOW, ServiceNowOperation.UPDATE_RECORD): ToolOperationRisk.WRITE,
    (ToolKind.SERVICENOW, ServiceNowOperation.CREATE_RECORD): ToolOperationRisk.WRITE,
    (ToolKind.SERVICENOW, ServiceNowOperation.DELETE_RECORD): ToolOperationRisk.DESTRUCTIVE,
}


def tool_kind(operation: ToolOperation) -> ToolKind:
    if isinstance(operation, JiraOperation):
        return ToolKind.JIRA
    if isinstance(operation, GitOperation):
        return ToolKind.GIT
    if isinstance(operation, ServiceNowOperation):
        return ToolKind.SERVICENOW
    raise ValueError("unknown tool operation")


def operation_risk(operation: ToolOperation) -> ToolOperationRisk:
    if not isinstance(operation, (JiraOperation, GitOperation, ServiceNowOperation)):
        raise ValueError("unknown tool operation")
    return _RISK[(tool_kind(operation), operation.value)]


def required_permission(operation: ToolOperation) -> ToolPermission:
    kind = tool_kind(operation)
    risk = operation_risk(operation)
    if kind is ToolKind.JIRA:
        return (
            ToolPermission.JIRA_READ
            if risk is ToolOperationRisk.READ
            else ToolPermission.JIRA_WRITE
        )
    if kind is ToolKind.GIT:
        return (
            ToolPermission.GIT_READ if risk is ToolOperationRisk.READ else ToolPermission.GIT_WRITE
        )
    return (
        ToolPermission.SERVICENOW_READ
        if risk is ToolOperationRisk.READ
        else ToolPermission.SERVICENOW_WRITE
    )
