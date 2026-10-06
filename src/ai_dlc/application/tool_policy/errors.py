"""Expected enforcement and infrastructure failures."""

from .models import ToolPolicyDecision, ToolPolicyEffect


class ToolPolicyDeniedError(Exception):
    def __init__(self, decision: ToolPolicyDecision) -> None:
        if decision.effect is not ToolPolicyEffect.DENY:
            raise ValueError("denial error requires a DENY decision")
        self.decision = decision
        super().__init__(f"tool policy denied: {decision.reason.value}")


class ToolApprovalRequiredError(Exception):
    def __init__(self, decision: ToolPolicyDecision) -> None:
        if decision.effect is not ToolPolicyEffect.REQUIRE_APPROVAL:
            raise ValueError("approval error requires a REQUIRE_APPROVAL decision")
        self.decision = decision
        super().__init__("tool operation requires approval")


class ToolPolicyAuditError(Exception):
    def __init__(self) -> None:
        super().__init__("tool policy audit recording failed")


class ToolPolicyConfigurationError(Exception):
    def __init__(self) -> None:
        super().__init__("tool policy lookup failed")
