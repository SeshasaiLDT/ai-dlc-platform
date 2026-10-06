"""Expected approval failures; messages never contain operation content or secrets."""


class ApprovalError(Exception):
    pass


class ApprovalNotFoundError(ApprovalError):
    def __init__(self) -> None:
        super().__init__("approval not found")


class ApprovalConflictError(ApprovalError):
    def __init__(self) -> None:
        super().__init__("approval version or idempotency conflict")


class InvalidApprovalSourceError(ApprovalError):
    def __init__(self) -> None:
        super().__init__("trusted tool policy did not require approval")


class ApprovalNotAuthorizedError(ApprovalError):
    def __init__(self) -> None:
        super().__init__("principal cannot decide this approval")


class SelfApprovalNotAllowedError(ApprovalNotAuthorizedError):
    def __init__(self) -> None:
        super().__init__()


class ApprovalInvalidTransitionError(ApprovalError):
    def __init__(self) -> None:
        super().__init__("approval is already terminal")


class ApprovalPersistenceError(ApprovalError):
    def __init__(self) -> None:
        super().__init__("approval persistence failed")


class ApprovalAuditError(ApprovalPersistenceError):
    def __init__(self) -> None:
        super().__init__()
