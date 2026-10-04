# ADR-001: Two-Repository Source Strategy

Status: Accepted

## Context
AI-DLC needs independent frontend and backend/agent development without creating repository sprawl.

## Decision
Use:
- `ai-dlc-platform` for backend, control plane, agents, harness, integrations, persistence, and infrastructure.
- `ai-dlc-ui` for the web frontend.

AgentCore runtime separation remains a deployment concern, not a source-repository concern.

## Alternatives considered
### Single repository
Rejected for now because UI and backend have sufficiently different toolchains and release concerns to justify separation.

### Repository per agent
Rejected because it creates unnecessary CI/CD, versioning, dependency-management, and local-development complexity.

## Consequences
Positive:
- simple mental model
- easy portfolio presentation
- shared backend contracts remain easy to refactor atomically
- independent frontend lifecycle

Tradeoff:
- backend repository contains multiple independently deployed runtimes

## Revisit triggers
Revisit only if ownership, access control, repository scale, or release independence materially justify more repositories.
