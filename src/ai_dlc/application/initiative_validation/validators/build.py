"""Non-executing build and test readiness checks."""

from ai_dlc.domain.initiative import InitiativeProfile

from ..models import ValidationFinding, ValidationSeverity


class BuildReadinessValidator:
    name = "build_readiness"

    def validate(self, profile: InitiativeProfile) -> tuple[ValidationFinding, ...]:
        if not profile.policies.code_generation.enabled:
            return ()
        if not profile.build_profiles:
            return (
                ValidationFinding(
                    code="code_generation_without_build_profiles",
                    severity=ValidationSeverity.WARNING,
                    path="build_profiles",
                    message="Code generation is enabled without a build profile for verification.",
                    validator=self.name,
                ),
            )
        if not any(build.test_command for build in profile.build_profiles):
            return (
                ValidationFinding(
                    code="code_generation_without_test_command",
                    severity=ValidationSeverity.WARNING,
                    path="build_profiles",
                    message=(
                        "Code generation is enabled, but no build profile defines a test command."
                    ),
                    validator=self.name,
                ),
            )
        return ()
