# ADR-003: A2A for Independent Agent Communication

Status: Proposed

## Decision
Use A2A across independently deployable agent boundaries.

Do not use A2A for:
- ordinary helper functions
- deterministic local workflow steps
- direct database operations
- tool calls that belong behind MCP or local execution

## Consequences
Every independent agent must publish a versioned capability contract and implement standardized correlation, timeout, cancellation, and error behavior through the shared harness.
