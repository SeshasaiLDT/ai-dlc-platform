# AI-DLC Platform

AI-DLC is an initiative-agnostic agent platform for software delivery and operational workflows.

This repository contains:
- backend APIs and control-plane services
- reusable agents
- the shared agent harness / SDK
- A2A contracts
- MCP integration adapters
- model routing
- persistence and workspace state
- AWS infrastructure
- initiative configuration

## Architectural rule

A new initiative must be onboardable without changing shared orchestrator, harness, or capability-agent source code.

## Planned runtime

- AWS Bedrock AgentCore Runtime for agents
- A2A for independent agent-to-agent communication
- MCP for governed enterprise tool access where appropriate
- Amazon RDS for PostgreSQL
- pgvector for semantic retrieval
- DynamoDB only for workloads that justify it after measurement

## Repository boundaries

The UI is maintained separately in `ai-dlc-ui`.

This repository intentionally keeps all backend and agent code together while preserving independent deployment boundaries for each AgentCore runtime.

## Initiative configuration

The first implemented contract is the versioned Initiative Profile schema. See
[Initiative Profiles](configs/initiatives/README.md) for examples, validation,
and JSON Schema export.

The [Initiative Registry](docs/architecture/initiative-registry.md) is the
application boundary for creating, reading, updating, listing, and disabling
registered profiles. Its current storage and mutation-event adapters are
in-memory implementations for tests and local development.

[Initiative readiness validation](docs/architecture/initiative-validation.md)
checks a validated profile's policy and build consistency and accepts injected
validators for future external resource checks. It runs independently of the
Registry and does not activate an initiative.

[Initiative configuration versioning](docs/architecture/initiative-versioning.md)
retains immutable revision history. Rollback appends a new revision while
preserving previous profiles and lifecycle status.

[Initiative onboarding](docs/architecture/initiative-onboarding.md) composes
profile loading, readiness validation, and Registry creation. It rejects
readiness errors before creating an active registration and revision 1.

[Atlas Travel](docs/architecture/sample-initiative.md) is the canonical
synthetic initiative fixture for future integration tests and demos. It is
separate from the smaller Initiative Profile schema examples.
