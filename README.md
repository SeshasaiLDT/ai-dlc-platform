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
