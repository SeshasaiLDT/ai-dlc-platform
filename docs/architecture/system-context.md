# AI-DLC System Context

Status: Draft

## High-level flow

User
→ AI-DLC UI
→ Backend API / Control Plane
→ Initiative + Authorization Resolution
→ Orchestrator
→ A2A
→ Capability Agents
→ Shared Agent Harness
→ Models / Governed Tools / Knowledge Retrieval / Local Workspaces
→ Artifacts / Results
→ User

## External systems

Initial integrations:
- Jira
- ServiceNow
- Git providers

Future integrations may be added without changing capability-agent contracts.

## AWS responsibilities

Planned AWS components:
- Bedrock AgentCore Runtime for independently deployable agents
- AgentCore Gateway where appropriate for governed MCP tool access
- Bedrock model access / model routing
- DynamoDB for operational platform state such as initiatives, revisions, workspaces, tasks, approvals, sessions, and execution/checkpoint state
- Amazon S3 for durable artifact bodies and large immutable outputs
- RDS PostgreSQL with pgvector for semantic knowledge retrieval
- standard AWS observability, identity, networking, and secrets services

## Identity, initiative, and resource resolution

Authenticated identity does not directly hardcode a Jira board, repository, or pgvector table.

The control plane resolves:

1. authenticated principal
2. initiative memberships available to that principal
3. selected initiative/workspace
4. initiative-owned logical scopes such as Jira projects, repositories, and knowledge source IDs
5. authorization/policy restrictions for that user
6. environment-specific physical resource bindings

For example, one user may be a member of two initiatives with different Jira
scopes and knowledge sources. Agents receive only the authorized logical context
needed for the selected task; trusted tool infrastructure retains binding and
credential details. Agents do not contain user-to-project mappings.

The [enterprise tool contract](enterprise-tool-contract.md) defines the separate
agent request and trusted execution context, normalized results, and MCP/direct/
local execution boundary.

## Resource bindings

Logical initiative configuration is separated from physical infrastructure.

Examples:
- a logical code knowledge source may bind to one pgvector table/namespace today and a company-wide code cluster later
- document sources may bind to a separate pgvector instance
- a Jira logical integration may bind to an approved Jira site/connection while its project keys remain initiative-scoped

Changing physical topology must not require capability-agent source changes.

## Data ownership

The control plane owns:
- initiatives and immutable configuration revisions
- memberships and authorization context
- workspaces and sessions
- task/plan/checkpoint state
- approvals
- artifact metadata and provenance
- audit metadata
- model/tool execution metadata
- logical-to-physical resource bindings

S3 owns canonical artifact bodies.

The knowledge retrieval layer owns embeddings, chunks, vector indexes, and retrieval metadata. It is not authoritative for workflow/application state.
