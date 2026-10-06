# Initiative configuration readiness validation

An Initiative Profile is a versioned configuration contract. AIDLC-19 parses YAML
and rejects structural and domain errors, including missing identity, invalid values,
duplicate IDs, and broken internal references. AIDLC-21 accepts only a validated
`InitiativeProfile` and evaluates whether its configuration is ready for platform
use. It returns a report; it does not activate or register the initiative.

```python
from ai_dlc.application.initiative_validation import InitiativeValidationService
from ai_dlc.domain.initiative import load_initiative_profile

profile = load_initiative_profile("configs/initiatives/examples/travel-platform.yaml")
report = InitiativeValidationService().validate(profile)
if not report.is_valid:
    for error in report.errors:
        print(error.path, error.code, error.message)
```

`ValidationFinding` has a stable machine-readable code, a human-readable message,
a configuration path when applicable, a severity, and the validator's identifier.
`ValidationReport` contains the initiative ID, an immutable tuple of findings,
and a timezone-aware UTC validation timestamp. Any `ERROR` makes `is_valid` false;
`WARNING` and `INFO` do not. Findings are sorted by severity (error, warning,
info), path, code, validator, then message, regardless of validator order.

## Built-in deterministic rules

| Condition | Code | Severity | Reason |
| --- | --- | --- | --- |
| Git, Jira, or ServiceNow writes enabled while its integration is disabled | `write_policy_requires_integration` | Error | The operation has no configured integration. |
| Human approval required on a disabled Git, Jira, or ServiceNow write policy | `approval_on_disabled_write_policy` | Warning | The setting is currently unused. |
| Repository requests `read_write` while Git writes are disabled | `repository_write_access_unused` | Warning | The repository may still be useful for read-only work; declared access is broader than the active policy. |
| Code generation enabled with no build profiles | `code_generation_without_build_profiles` | Warning | Generated work has no configured build or test profile. |
| Code generation enabled with profiles but no test command in any profile | `code_generation_without_test_command` | Warning | Generated work lacks a configured verification command. |

Code generation does not require Git writes; a later workflow may return a patch.
The validator does not repeat AIDLC-19 field, reference, or version checks and
does not execute build commands.

## External validation boundary

`InitiativeConfigurationValidator` is a synchronous port with a stable `name`
and `validate(profile) -> tuple[ValidationFinding, ...]`. The service always runs
its built-in validators and accepts additional validators through its constructor.
Future adapters can check enabled Jira projects, ServiceNow assignment groups
and scopes, Git repositories and requested access, or enabled knowledge sources.
They should report missing or inaccessible resources at the corresponding
configuration path. The service itself makes no Jira, ServiceNow, Git, knowledge,
AWS, or database calls. No adapter is discovered globally.

An unexpected validator exception or invalid return becomes an `ERROR` finding
with code `validator_execution_failed`; the exception detail is not exposed in
the report. The service's injected clock follows the Registry's UTC clock
pattern. A caller may validate a profile before a future onboarding/activation
decision, but Registry `create` and `update` remain independent of readiness
validation. Live adapter wiring, authorization, activation, and persistence are
later work.
