# Canonical sample initiative: Atlas Travel

Atlas Travel is a stable, fictional software initiative for integration tests,
local demonstrations, and future AI-DLC capability development. It is
deliberately unrelated to POS or any company-specific system. Its configuration
is [atlas-travel.yaml](../../configs/initiatives/samples/atlas-travel.yaml),
separate from the smaller schema demonstrations under `examples/`. Treat it as
a platform fixture, not a production initiative.

The sample imagines a trip-planning product with a React/TypeScript web app, a
Python/FastAPI API, an application PostgreSQL database, saved trips, shared
itineraries, weather forecasts, accounts, and notifications. These are scenario
descriptions for future tests and demos; this ticket creates no application or
database. The Initiative Profile includes only metadata supported by schema
version `1.0`.

| Area | Canonical configuration |
| --- | --- |
| Identity and ownership | `atlas-travel`, owned by Atlas Engineering with Atlas Product support |
| Git | Synthetic `example-org/atlas-travel-web` and `example-org/atlas-travel-api` on `main`, both requesting `read_write` access |
| Jira | Enabled for the synthetic `ATLAS` project only |
| ServiceNow | Disabled |
| Knowledge | Synthetic architecture docs, product requirements, and codebase identifiers; no retrieval or indexing |
| Build profiles | `api-python` installs Python dependencies and runs `pytest`; `web-typescript` installs, builds, and tests with npm |
| Policies | Code analysis and generation enabled; Git writes enabled with human approval; Jira and ServiceNow writes disabled |
| Model defaults | Logical routing, reasoning, complex reasoning, and reviewer roles only |

The owner, repositories, Jira key, knowledge identifiers, and product are
synthetic. They do not assert that GitHub repositories, Jira projects, documents,
or integrations exist. Current readiness validation checks deterministic policy
and build consistency; future live validators must check external existence and
access before using this sample against real systems. Never replace these
identifiers with proprietary names, credentials, internal URLs, or company data.

The sample should remain stable enough for Registry, onboarding, readiness,
revision, persistence, Workspace, orchestration, Change Impact, Code Analysis,
Implementation, Verification, UI, and portfolio tests. Future test scenarios
may refer to fictional saved-trip, itinerary-sharing, weather, and notification
work without creating actual Jira tickets in this repository. AIDLC-19 loading,
AIDLC-21 readiness validation, and AIDLC-23 onboarding of this file are covered
by automated tests; the Registry creates revision 1 without platform source
changes.
