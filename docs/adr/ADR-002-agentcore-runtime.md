# ADR-002: AWS Bedrock AgentCore Runtime

Status: Proposed

## Context
AI-DLC requires independently deployable, observable agents capable of model/tool use and potentially long-running execution.

## Decision
Use AWS Bedrock AgentCore Runtime as the default runtime for independent AI-DLC agents.

Each capability agent may deploy separately even though source code lives in `ai-dlc-platform`.

## Alternatives considered
- Lambda-only execution
- ECS/Fargate
- EC2-hosted services
- one shared process for all agents

## Consequences
The platform must define:
- runtime packaging standards
- identity and permissions per runtime
- health/version metadata
- A2A service contracts
- deployment automation
- observability
