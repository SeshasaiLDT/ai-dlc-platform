# ADR-005: Model Roles Instead of Hardcoded Models

Status: Proposed

## Decision
Define four logical model roles:
- Routing
- Standard Reasoning
- Deep Reasoning
- Independent Reviewer

Concrete provider/model names are configuration and may change without agent-source changes.

Deterministic routing precedes classifier-LLM routing whenever possible.

## Consequences
The control plane requires:
- model registry
- role eligibility
- fallback policy
- cost/usage tracking
- security/data eligibility
- escalation rules
