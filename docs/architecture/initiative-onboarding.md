# Initiative onboarding (AIDLC-23)

`InitiativeOnboardingService` is deterministic application logic that composes
the existing Initiative Profile loader, readiness validation service, and
Initiative Registry. It does not parse YAML itself, evaluate readiness rules,
or construct configuration revisions.
The Profile already carries integration/tool scopes, repositories, knowledge
sources, build profiles, permissions, model-role defaults, and policies; the
workflow passes that same typed configuration through validation and registration.

The sequence is:

1. `onboard_from_file(path)` loads and schema-validates the YAML using AIDLC-19.
   `onboard(profile)` starts with an already validated `InitiativeProfile`.
2. AIDLC-21 readiness validation returns a machine-readable `ValidationReport`.
3. Any `ERROR` finding produces an immutable `REJECTED` result containing the
   exact report. The Registry is not called, so no registration, revision, or
   mutation event is created. Warnings do not block onboarding.
4. If the report is valid, the workflow calls `InitiativeRegistry.create(profile)`.
   The Registry creates an `ACTIVE` registration, configuration revision 1, and
   its one existing creation event. The immutable `SUCCEEDED` result retains
   both registration and the exact readiness report, including warnings.

```python
from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.application.initiative_onboarding import InitiativeOnboardingService
from ai_dlc.application.initiative_validation import InitiativeValidationService
from ai_dlc.application.initiatives import InitiativeRegistry

registry = InitiativeRegistry(InMemoryInitiativeRepository(), InMemoryRegistryEventSink())
onboarding = InitiativeOnboardingService(registry, InitiativeValidationService())
result = onboarding.onboard_from_file("configs/initiatives/examples/travel-platform.yaml")
assert result.succeeded and result.current_revision == 1
```

File read, malformed YAML, and schema/domain failures retain AIDLC-19's typed
`ProfileValidationError`. Duplicate initiative IDs retain the Registry's
`InitiativeAlreadyExistsError`; onboarding never silently updates or reuses an
existing registration. Unexpected service or storage failures are not converted
to ordinary readiness rejection. This keeps invalid configuration distinct
from operational failure.

The validation service is injected. A future composition root may attach live
Jira, Git, ServiceNow, or knowledge validators through AIDLC-21's validator
port without changing onboarding code. This ticket makes no live calls and
adds no agents, workflow engine, onboarding audit system, or new Registry
lifecycle state. Successful onboarding means registered and active after all
currently configured readiness checks pass; a separate enterprise activation
process may be designed later if requirements call for one.
