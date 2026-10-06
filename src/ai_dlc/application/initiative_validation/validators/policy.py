"""Readiness relationships between integration configuration and write policies."""

from ai_dlc.domain.initiative import InitiativeProfile
from ai_dlc.domain.initiative.enums import RepositoryAccess

from ..models import ValidationFinding, ValidationSeverity


class PolicyConsistencyValidator:
    name = "policy_consistency"

    def validate(self, profile: InitiativeProfile) -> tuple[ValidationFinding, ...]:
        findings: list[ValidationFinding] = []
        for integration, policy in (
            ("git", "git_write"),
            ("jira", "jira_write"),
            ("servicenow", "servicenow_write"),
        ):
            operation = getattr(profile.policies, policy)
            configured = getattr(profile.integrations, integration)
            if operation.enabled and not configured.enabled:
                findings.append(
                    ValidationFinding(
                        code="write_policy_requires_integration",
                        severity=ValidationSeverity.ERROR,
                        path=f"policies.{policy}.enabled",
                        message=f"Enable the {integration} integration or disable {policy} writes.",
                        validator=self.name,
                    )
                )
            if not operation.enabled and operation.human_approval_required:
                findings.append(
                    ValidationFinding(
                        code="approval_on_disabled_write_policy",
                        severity=ValidationSeverity.WARNING,
                        path=f"policies.{policy}.human_approval_required",
                        message=f"Approval is configured, but {policy} writes are disabled.",
                        validator=self.name,
                    )
                )

        if not profile.policies.git_write.enabled:
            for index, repository in enumerate(profile.integrations.git.repositories):
                if repository.access == RepositoryAccess.READ_WRITE:
                    findings.append(
                        ValidationFinding(
                            code="repository_write_access_unused",
                            severity=ValidationSeverity.WARNING,
                            path=f"integrations.git.repositories[{index}].access",
                            message=(
                                "Repository requests read_write access, but the Git write policy "
                                "is disabled. Readiness may still be valid for read-only use."
                            ),
                            validator=self.name,
                        )
                    )
        return tuple(findings)
