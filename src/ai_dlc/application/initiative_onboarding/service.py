"""Deterministic composition of profile loading, readiness, and registration."""

from pathlib import Path

from ai_dlc.application.initiative_validation import InitiativeValidationService
from ai_dlc.application.initiatives import InitiativeRegistry
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile

from .models import InitiativeOnboardingResult, OnboardingStatus


class InitiativeOnboardingService:
    def __init__(
        self, registry: InitiativeRegistry, validation_service: InitiativeValidationService
    ) -> None:
        self._registry = registry
        self._validation_service = validation_service

    def onboard_from_file(self, path: str | Path) -> InitiativeOnboardingResult:
        """Load with AIDLC-19; its typed ProfileValidationError remains public."""
        return self.onboard(load_initiative_profile(Path(path)))

    def onboard(self, profile: InitiativeProfile) -> InitiativeOnboardingResult:
        """Reject readiness errors before creating an active Registry registration."""
        report = self._validation_service.validate(profile)
        initiative_id = profile.initiative.id
        if not report.is_valid:
            return InitiativeOnboardingResult(
                status=OnboardingStatus.REJECTED,
                initiative_id=initiative_id,
                validation_report=report,
                registration=None,
            )

        registration = self._registry.create(profile)
        return InitiativeOnboardingResult(
            status=OnboardingStatus.SUCCEEDED,
            initiative_id=initiative_id,
            validation_report=report,
            registration=registration,
        )
