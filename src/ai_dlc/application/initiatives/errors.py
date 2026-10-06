"""Public Initiative Registry failures."""


class InitiativeRegistryError(Exception):
    """Base class for expected Registry operation failures."""


class InitiativeAlreadyExistsError(InitiativeRegistryError):
    def __init__(self, initiative_id: str) -> None:
        super().__init__(f"initiative '{initiative_id}' already exists")


class InitiativeNotFoundError(InitiativeRegistryError):
    def __init__(self, initiative_id: str) -> None:
        super().__init__(f"initiative '{initiative_id}' was not found")


class InitiativeIdentityMismatchError(InitiativeRegistryError):
    def __init__(self, initiative_id: str, profile_id: str) -> None:
        super().__init__(
            f"cannot update initiative '{initiative_id}' with profile for '{profile_id}'"
        )
