"""Port for deterministic or externally backed readiness checks."""

from typing import Protocol

from ai_dlc.domain.initiative import InitiativeProfile

from .models import ValidationFinding


class InitiativeConfigurationValidator(Protocol):
    @property
    def name(self) -> str: ...

    def validate(self, profile: InitiativeProfile) -> tuple[ValidationFinding, ...]: ...
