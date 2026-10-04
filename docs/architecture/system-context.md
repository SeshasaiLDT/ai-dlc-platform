# AI-DLC System Context

Status: Draft

## High-level flow

User
→ AI-DLC UI
→ Backend API / Control Plane
→ Orchestrator
→ A2A
→ Capability Agents
→ Shared Agent Harness
→ Models / MCP Tools / Local Workspaces
→ Artifacts / Results
→ User

## External systems

Initial integrations:
- Jira
- ServiceNow
- GitHub

Future integrations may be added without changing capability-agent contracts.

## AWS responsibilities

Planned AWS components:
- Bedrock AgentCore Runtime for independently deployable agents
- AgentCore Gateway where appropriate for governed MCP tool access
- Bedrock model access / model routing
- RDS PostgreSQL for platform persistence
- pgvector extension for semantic/vector retrieval
- object storage for large immutable artifacts where appropriate
- standard AWS observability, identity, networking, and secrets services

## Data ownership

The control plane owns:
- initiatives
- memberships
- workspaces
- task state
- plans
- approvals
- artifact metadata
- audit metadata
- model/tool execution metadata

Vector retrieval stores embeddings and associated retrieval metadata, not platform authority.
