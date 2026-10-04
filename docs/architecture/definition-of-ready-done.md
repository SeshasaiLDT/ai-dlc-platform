# Definition of Ready and Definition of Done

Status: Draft  
Jira: AIDLC-18

These standards apply to AI-DLC Jira Stories unless explicitly documented otherwise.

# Definition of Ready

A Story may enter active implementation only when all applicable items below are satisfied.

## Problem and Outcome

- The problem being solved is clear.
- The expected user/platform outcome is stated.
- The Story has measurable acceptance criteria.
- The Story is small enough to complete within a normal development cycle.
- If the Story is too large, it has been decomposed before implementation.

## Architecture

- Relevant architecture principles have been reviewed.
- Architectural boundaries affected by the Story are identified.
- Any new major architectural decision has an ADR or explicitly does not require one.
- Initiative-specific behavior has been identified and kept out of shared platform code.
- Data ownership is clear.
- Agent vs tool vs skill vs deterministic-code responsibility is clear.

## Dependencies

- Blocking Jira issues are identified.
- Required external systems/services are known.
- Required credentials, sandbox access, or AWS services are available or mocked.
- Upstream contracts required for implementation are stable enough to consume.

## Data

If persistent data changes are involved:
- entities/fields being introduced or changed are identified
- migration impact is understood
- authoritative data store is identified
- retention/sensitivity requirements are considered

If retrieval/vector behavior changes:
- source ownership is defined
- chunk/embedding metadata requirements are defined
- initiative isolation requirements are clear

## Security and Authorization

- User/agent permissions required by the Story are known.
- Read vs write vs destructive operations are classified.
- Human approval requirements are identified.
- Secrets/credentials required are identified without placing them in Jira or source.

## Agent/LLM Work

If an LLM or agent is involved:
- model role is identified instead of hardcoding a model name
- required tools are identified
- expected structured output is defined
- maximum iteration/retry behavior is known
- evaluation approach is defined
- deterministic alternatives have been considered

## Testing

- Unit-test expectations are identified.
- Integration/contract-test needs are identified.
- Acceptance criteria are testable.
- Any required evaluation dataset/case is identified.

## Observability

For runtime behavior:
- meaningful logs/metrics/traces required are understood
- failure states are identified
- important actions can be correlated to task/workspace identifiers

## AI Coding Assistant Handoff

Before Codex or Claude Code begins implementation, the prompt/context must include:
- Jira ticket ID and acceptance criteria
- relevant architecture principles
- affected ADRs
- explicit in-scope and out-of-scope boundaries
- existing contracts to preserve
- required tests
- instruction not to introduce unrelated architectural changes

If those inputs are missing, the Story is not Ready.

---

# Definition of Done

A Story is Done only when all applicable criteria below are satisfied.

## Implementation

- All acceptance criteria are satisfied.
- Code follows existing architecture and repository boundaries.
- No initiative-specific constants were introduced into shared platform code.
- No unnecessary framework/service/database/protocol was added.
- Dead code, temporary hacks, and debug-only behavior are removed or explicitly ticketed.

## Code Quality

- Code is readable and maintainable.
- Public/shared contracts are typed or schema-defined.
- Error handling is explicit.
- Retry behavior is bounded.
- Idempotency is handled for retryable write operations where applicable.

## Testing

- New/changed logic has appropriate unit tests.
- Relevant contract tests pass.
- Relevant integration tests pass.
- Relevant end-to-end tests pass.
- Agent/evaluation cases pass where applicable.
- Regression tests exist for fixed defects.

## Security

- Authorization is enforced server-side.
- No credentials/secrets are committed.
- Tool permissions respect read/write/destructive classification.
- Human approval gates function where required.
- Sensitive data is not unnecessarily logged.

## Data

If persistence changed:
- migration exists and is tested
- rollback/compatibility impact is understood
- constraints/indexes are appropriate
- pgvector metadata supports initiative isolation where applicable

## Observability

- Important operations emit structured logs.
- Correlation/task/workspace identifiers are propagated.
- Relevant metrics/traces are emitted.
- Failures provide actionable diagnostics without leaking secrets.

## Documentation

- Relevant architecture or operational docs are updated.
- New architectural decisions have ADRs.
- Configuration/schema changes are documented.
- README/setup documentation remains accurate.

## Git and Review

- Work is associated with its Jira Story.
- Changes are committed to a short-lived feature branch.
- Pull request explains what changed and how it was tested.
- Required review is complete.
- CI checks pass.
- No unresolved blocking review comments remain.

## Deployment

When deployment is part of the Story:
- AWS Dev deployment succeeds.
- Smoke tests pass.
- Environment configuration is externalized.
- Rollback path is known.

For Demo promotion:
- relevant E2E/evaluation tests pass
- release is versioned
- no known critical/high severity issue remains

## Jira Completion Evidence

Before marking the Jira Story complete, record enough evidence to answer:
- What changed?
- Where is the implementation?
- How was it tested?
- Were any ADRs changed/added?
- Are there known limitations or follow-up tickets?

A Story is not Done merely because generated code compiles or an agent returns a plausible answer.
