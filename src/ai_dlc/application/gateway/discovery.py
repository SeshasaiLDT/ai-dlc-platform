"""Pure least-privilege discovery; application services still authorize calls."""

from dataclasses import dataclass
from fnmatch import fnmatchcase

from ai_dlc.application.tool_policy import ToolKind, ToolOperationRisk, ToolPolicyEffect
from ai_dlc.application.tool_policy.ports import ToolPolicyRepository
from ai_dlc.domain.initiative.enums import RepositoryAccess

from .catalog import GatewayCatalog, GatewayTool
from .context import TrustedGatewayContext


@dataclass(frozen=True, slots=True)
class AvailableTool:
    tool: GatewayTool
    approval_required: bool

    def public_definition(self) -> dict[str, object]:
        return self.tool.public_definition(approval_required=self.approval_required)


class GatewayDiscovery:
    def __init__(self, catalog: GatewayCatalog, policies: ToolPolicyRepository) -> None:
        self._catalog = catalog
        self._policies = policies

    def available_tools(self, context: TrustedGatewayContext) -> tuple[AvailableTool, ...]:
        if not isinstance(context, TrustedGatewayContext):
            raise TypeError("trusted gateway context required")
        auth = context.resolution.authorization
        profile = context.resolution.profile
        result = []
        for tool in self._catalog.tools:
            if tool.permission not in auth.tool_permissions:
                continue
            integration = {
                ToolKind.JIRA: profile.integrations.jira,
                ToolKind.SERVICENOW: profile.integrations.servicenow,
                ToolKind.GIT: profile.integrations.git,
            }[tool.domain]
            if not integration.enabled:
                continue
            if tool.risk is not ToolOperationRisk.READ:
                write_policy = {
                    ToolKind.JIRA: profile.policies.jira_write,
                    ToolKind.SERVICENOW: profile.policies.servicenow_write,
                    ToolKind.GIT: profile.policies.git_write,
                }[tool.domain]
                if not write_policy.enabled:
                    continue
            else:
                write_policy = None
            scopes = {
                ToolKind.JIRA: auth.allowed_scopes.jira_projects,
                ToolKind.SERVICENOW: auth.allowed_scopes.servicenow_scopes,
                ToolKind.GIT: auth.allowed_scopes.repository_ids,
            }[tool.domain]
            if tool.domain is ToolKind.GIT and tool.risk is not ToolOperationRisk.READ:
                scopes = frozenset(
                    repo.id
                    for repo in profile.integrations.git.repositories
                    if repo.id in scopes and repo.access is RepositoryAccess.READ_WRITE
                )
            if not scopes:
                continue
            rules = self._policies.policies_for(auth.initiative_id, tool.domain)
            if any(rule.initiative_id != auth.initiative_id for rule in rules):
                raise ValueError("cross-initiative policy repository result")
            approval = False
            visible = False
            for scope in scopes:
                applicable = [
                    rule
                    for rule in rules
                    if type(rule.operation) is type(tool.operation)
                    and rule.operation == tool.operation
                    and rule.target_id in (None, scope)
                    and (
                        rule.branch_pattern is None
                        or tool.domain is ToolKind.GIT
                        and any(
                            fnmatchcase(repo.default_branch, rule.branch_pattern)
                            for repo in profile.integrations.git.repositories
                            if repo.id == scope
                        )
                    )
                ]
                if len(applicable) == 1 and applicable[0].effect is not ToolPolicyEffect.DENY:
                    visible = True
                    approval = approval or applicable[0].effect is ToolPolicyEffect.REQUIRE_APPROVAL
            if visible:
                result.append(
                    AvailableTool(
                        tool,
                        approval or bool(write_policy and write_policy.human_approval_required),
                    )
                )
        return tuple(result)
