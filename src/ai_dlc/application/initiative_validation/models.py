"""Immutable results of initiative configuration readiness checks."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    severity: ValidationSeverity
    path: str | None
    message: str
    validator: str

    def __post_init__(self) -> None:
        if not isinstance(self.severity, ValidationSeverity):
            raise TypeError("finding severity must be a ValidationSeverity")
        for field_name in ("code", "message", "validator"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"finding {field_name} must be a nonblank string")
        if self.path is not None and (not isinstance(self.path, str) or not self.path.strip()):
            raise ValueError("finding path must be a nonblank string or None")


@dataclass(frozen=True, slots=True)
class ValidationReport:
    initiative_id: str
    findings: tuple[ValidationFinding, ...]
    validated_at: datetime

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def errors(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == ValidationSeverity.ERROR)

    @property
    def warnings(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == ValidationSeverity.WARNING)

    @property
    def info(self) -> tuple[ValidationFinding, ...]:
        return tuple(f for f in self.findings if f.severity == ValidationSeverity.INFO)
