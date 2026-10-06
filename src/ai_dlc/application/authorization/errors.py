"""Authorization enforcement and audit failures."""

from .decisions import AuthorizationDecision


class AuthorizationDeniedError(Exception):
    def __init__(self, decision: AuthorizationDecision) -> None:
        if decision.allowed:
            raise ValueError("cannot raise denial for an allowed decision")
        self.decision = decision
        super().__init__(f"authorization denied: {decision.reason.value}")


class AuthorizationAuditError(Exception):
    def __init__(self) -> None:
        super().__init__("authorization audit recording failed")
