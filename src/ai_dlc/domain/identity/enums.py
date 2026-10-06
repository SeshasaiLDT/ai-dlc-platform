"""Provider-neutral authorization identifiers."""

from enum import StrEnum


class Role(StrEnum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    DEVELOPER = "developer"
    REVIEWER = "reviewer"
    INITIATIVE_ADMIN = "initiative_admin"
    PLATFORM_ADMIN = "platform_admin"


class Capability(StrEnum):
    INVESTIGATION = "investigation"
    CHANGE_IMPACT = "change_impact"
    CODE_ANALYSIS = "code_analysis"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"


class ToolPermission(StrEnum):
    JIRA_READ = "jira.read"
    JIRA_WRITE = "jira.write"
    GIT_READ = "git.read"
    GIT_WRITE = "git.write"
    SERVICENOW_READ = "servicenow.read"
    SERVICENOW_WRITE = "servicenow.write"
    KNOWLEDGE_READ = "knowledge.read"
    ARTIFACT_READ = "artifact.read"
    ARTIFACT_WRITE = "artifact.write"


class AdminPermission(StrEnum):
    INITIATIVE_MEMBERSHIP_MANAGE = "initiative.membership.manage"
    INITIATIVE_POLICY_MANAGE = "initiative.policy.manage"
    INITIATIVE_APPROVAL_MANAGE = "initiative.approval.manage"
    PLATFORM_MANAGE = "platform.manage"
