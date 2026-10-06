"""Typed, immutable outcome of one initiative onboarding attempt."""

from dataclasses import dataclass
from enum import StrEnum

from ai_dlc.application.initiative_validation import ValidationReport
from ai_dlc.application.initiatives import RegisteredInitiative


class OnboardingStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class InitiativeOnboardingResult:
    status: OnboardingStatus
    initiative_id: str
    validation_report: ValidationReport
    registration: RegisteredInitiative | None

    def __post_init__(self) -> None:
        if self.initiative_id != self.validation_report.initiative_id:
            raise ValueError("onboarding initiative ID must match the validation report")
        if self.status == OnboardingStatus.SUCCEEDED:
            if (
                self.registration is None
                or self.registration.id != self.initiative_id
                or not self.validation_report.is_valid
            ):
                raise ValueError(
                    "successful onboarding requires a matching registration and valid report"
                )
        elif self.status == OnboardingStatus.REJECTED:
            if self.registration is not None or self.validation_report.is_valid:
                raise ValueError("rejected onboarding requires errors and no registration")
        else:
            raise ValueError("unsupported onboarding status")

    @property
    def succeeded(self) -> bool:
        return self.status == OnboardingStatus.SUCCEEDED

    @property
    def current_revision(self) -> int | None:
        return self.registration.current_revision if self.registration is not None else None
