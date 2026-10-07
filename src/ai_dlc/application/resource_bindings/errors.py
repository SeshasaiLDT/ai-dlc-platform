"""Fail-closed binding registry failures."""


class ResourceBindingError(Exception):
    pass


class BindingExistsError(ResourceBindingError):
    pass


class BindingNotFoundError(ResourceBindingError):
    pass


class BindingConflictError(ResourceBindingError):
    pass


class BindingIsolationError(ResourceBindingError):
    pass


class BindingResolutionDeniedError(ResourceBindingError):
    pass


class BindingAuditError(ResourceBindingError):
    pass
