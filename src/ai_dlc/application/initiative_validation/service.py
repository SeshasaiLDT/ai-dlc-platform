"""Independent readiness validation for an already validated Initiative Profile."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from ai_dlc.domain.initiative import InitiativeProfile

from .models import ValidationFinding, ValidationReport, ValidationSeverity
from .ports import InitiativeConfigurationValidator
from .validators import BuildReadinessValidator, PolicyConsistencyValidator

_SEVERITY_ORDER = {
    ValidationSeverity.ERROR: 0,
    ValidationSeverity.WARNING: 1,
    ValidationSeverity.INFO: 2,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _finding_order(finding: ValidationFinding) -> tuple[int, str, str, str, str]:
    return (
        _SEVERITY_ORDER[finding.severity],
        finding.path or "",
        finding.code,
        finding.validator,
        finding.message,
    )


class InitiativeValidationService:
    """Runs built-in and injected validators without activating an initiative."""

    def __init__(
        self,
        validators: Iterable[InitiativeConfigurationValidator] = (),
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._validators = (PolicyConsistencyValidator(), BuildReadinessValidator(), *validators)
        self._clock = clock

    def validate(self, profile: InitiativeProfile) -> ValidationReport:
        if not isinstance(profile, InitiativeProfile):
            raise TypeError("Readiness validation requires a validated InitiativeProfile")

        validated_at = self._now()
        findings: list[ValidationFinding] = []
        for validator in self._validators:
            safe_name = type(validator).__name__
            try:
                name = validator.name
                if not isinstance(name, str) or not name.strip():
                    raise TypeError("validator name must be a nonblank string")
                safe_name = name
                result = validator.validate(profile)
                if not isinstance(result, tuple) or not all(
                    isinstance(finding, ValidationFinding) for finding in result
                ):
                    raise TypeError("validator must return a tuple of ValidationFinding")
                findings.extend(result)
            except Exception:
                # An adapter failure cannot be mistaken for a ready configuration.
                findings.append(
                    ValidationFinding(
                        code="validator_execution_failed",
                        severity=ValidationSeverity.ERROR,
                        path=None,
                        message=f"Validator '{safe_name}' could not complete its readiness check.",
                        validator=safe_name,
                    )
                )

        return ValidationReport(
            initiative_id=profile.initiative.id,
            findings=tuple(sorted(findings, key=_finding_order)),
            validated_at=validated_at,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Validation clock must return a timezone-aware datetime")
        return value.astimezone(UTC)
