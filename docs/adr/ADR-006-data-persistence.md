# ADR-006: PostgreSQL-First Persistence with pgvector

Status: Proposed

## Context
AI-DLC needs durable relational platform state and semantic retrieval. DynamoDB is also under consideration for selected high-throughput workloads.

## Decision
Start with Amazon RDS for PostgreSQL as the system of record and enable pgvector for vector search.

Use PostgreSQL initially for:
- initiatives and configuration versions
- memberships and policies
- workspaces
- tasks and plans
- approvals
- artifact metadata
- audit metadata
- model/tool execution metadata

Use pgvector for:
- embeddings
- semantic retrieval indexes
- chunk metadata and initiative-scoped retrieval

Do not introduce DynamoDB merely because task/session data is dynamic.

## Why
The initial domain is strongly relational and benefits from:
- transactions
- foreign keys
- joins
- unified migrations
- fewer persistence systems
- simpler local and portfolio development

## DynamoDB revisit criteria
Add DynamoDB only if a measured workload benefits materially from:
- very high write throughput
- simple known-key access patterns
- extreme horizontal scale
- TTL-heavy ephemeral state
- event/checkpoint access patterns that become awkward or expensive in PostgreSQL

If introduced, DynamoDB should own a clearly bounded workload rather than duplicate PostgreSQL authority.

## Consequences
The initial system has one authoritative database technology plus pgvector, reducing operational complexity.
