"""Fail-closed model registry failures."""


class ModelRegistryError(Exception):
    pass


class DeploymentExistsError(ModelRegistryError):
    pass


class DeploymentNotFoundError(ModelRegistryError):
    pass


class RevisionConflictError(ModelRegistryError):
    """The caller's expected revision is stale; the stored record is unchanged."""


class ModelRegistryAuditError(ModelRegistryError):
    """The required audit event could not be recorded, so the mutation was not applied."""
