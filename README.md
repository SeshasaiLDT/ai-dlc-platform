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

- AWS Bedrock for model inference
- AWS Bedrock AgentCore Runtime for independently deployable agents
- AgentCore Gateway for governed enterprise tool access where appropriate
- Amazon DynamoDB for operational platform state
- Amazon S3 for canonical artifacts and static UI assets
- Amazon RDS for PostgreSQL + pgvector for semantic retrieval
- Amazon ECR for agent container images
- Amazon CloudFront for frontend delivery
- Amazon API Gateway + Lambda as the preferred initial control-plane/API hosting model
- Route 53 / ACM for DNS and TLS where a custom domain is used
- IAM, Secrets Manager, KMS, CloudWatch, CloudTrail, and VPC controls for security and operations

ECS/Fargate remains an optional compute choice for workloads that materially benefit from persistent container execution. The React/Vite SPA should default to S3 + CloudFront rather than EC2/Fargate.

## Repository boundaries

The UI is maintained separately in `ai-dlc-ui`.

This repository intentionally keeps all backend and agent code together while preserving independent deployment boundaries for each AgentCore runtime.

## Architecture

[Identity, initiative, and resource resolution](docs/architecture/resource-resolution.md)
defines how authenticated users resolve to authorized initiatives and how logical
Jira/knowledge resources resolve to environment-specific physical resources.

[AWS deployment topology](docs/architecture/aws-deployment-topology.md) defines
the preferred AWS service boundaries and identifies which services are core
versus workload-dependent.

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

[Enterprise identity and initiative RBAC](docs/architecture/identity-rbac.md)
defines provider-neutral principals, memberships, explicit grants, and logical
scope narrowing for one selected initiative.

[Authentication integration boundary](docs/architecture/authentication-boundary.md)
defines credential validation and provider-neutral Principal propagation for
backend application calls.

[Centralized authorization service](docs/architecture/authorization-service.md)
defines audited server-side allow/deny decisions for capabilities, tools,
administration, and logical resource targets.

[Tool-permission policy](docs/architecture/tool-permission-policy.md)
defines initiative-specific operation rules layered after base tool authorization.
