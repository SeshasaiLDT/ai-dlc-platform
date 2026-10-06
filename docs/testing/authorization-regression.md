# Authorization regression suite

Status: AIDLC-30

## Purpose

`tests/security/` exercises the AIDLC-25 through AIDLC-29 boundaries together.
It uses synthetic enterprise principals, two independent Initiative Profiles,
in-memory policy and audit adapters, and the existing file-backed SQLite
approval adapter. It needs no AWS credentials, live agents, tools, or network.

## Threat scenarios

- Cross-initiative access by an unassigned user or by a user with different
  roles in each initiative.
- Read-to-write escalation across Jira, Git, and ServiceNow.
- Substituting a tool, operation, logical target, or policy set after a grant.
- Replaying an approval against another branch, repository, initiative,
  operation, or configuration revision.
- An agent or service caller attempting to exceed the authenticated user's
  delegated permissions, change risk classification, or approve an action.
- Self approval, platform-admin privilege leakage into initiative actions,
  disabled membership, missing grants, and missing policies.
- Audit outage at base authorization, tool policy, or durable approval commit.

## Security invariants

Later layers may narrow authorization but never expand it. An approval applies
to one exact operation only. A user's permissions are initiative-specific.
Agents cannot exceed the delegated human/platform authorization envelope.
Prompt text, caller-supplied permission claims, and fabricated decisions cannot
replace trusted server-side evaluation. The approval execution gate rechecks
current base authorization and tool policy. `APPROVED` alone never executes a
tool.

## CI and local use

`.github/workflows/security-regression.yml` runs on every pull request and on
pushes to `develop`. It installs the project with development dependencies and
runs Ruff lint, Ruff format check, and `pytest -q`. Pytest's normal discovery
includes all `tests/security/test_*.py` files, so the security suite cannot be
skipped by a separate marker or optional job. Run locally with:

```bash
pytest -q tests/security
pytest -q
ruff check .
ruff format --check .
```

The suite is a contract regression guard for future API, agent harness, and
tool-gateway integrations. Those entrypoints must use the same application
services before protected execution.
