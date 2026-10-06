# ADR-006: Purpose-Built Persistence by Data Type

Status: Accepted

## Context

AI-DLC has several materially different persistence workloads:

- operational application state such as sessions, workspaces, tasks, approvals, agent executions, and checkpoints
- durable generated artifacts such as design documents, implementation results, and verification reports
- semantic knowledge retrieval over code, documentation, release notes, and other indexed sources
- initiative registration and immutable configuration revisions

Using PostgreSQL for all of these workloads simply because pgvector is required for retrieval would couple unrelated platform state to the knowledge store.

The platform already uses logical Initiative Profile configuration so initiative-specific behavior does not leak into shared agent code. Persistence should follow the same separation.

## Decision

Use purpose-built persistence boundaries:

### DynamoDB — operational platform state

DynamoDB is the preferred operational system of record for:

- Initiative Registry records and configuration revision metadata
- user/initiative membership and authorization-supporting mappings where appropriate
- workspaces and sessions
- tasks, plans, checkpoints, and resumable execution state
- agent execution state
- approval state
- artifact metadata and relationships when access patterns remain key-oriented
- interaction/session history where retention requirements allow it

Data models must be designed from explicit access patterns. Conditional writes and transactions should enforce invariants such as immutable revision sequencing and stale-write protection.

### Amazon S3 — canonical artifact bodies

S3 stores larger or immutable artifact content such as:

- approved design artifacts
- implementation/change artifacts
- verification reports
- evidence bundles
- exported documents and generated files

Platform state stores artifact IDs, metadata, provenance, status, and S3 object references rather than duplicating large artifact bodies.

### Amazon RDS for PostgreSQL + pgvector — knowledge retrieval

PostgreSQL with pgvector is the semantic knowledge store, not the default application database.

Use it for:

- chunked code/document content used for retrieval
- embeddings
- vector indexes such as HNSW
- retrieval metadata
- provenance required to trace chunks to logical knowledge sources
- structured metadata needed to filter semantic searches by initiative, repository, source type, document, or other approved scope

The physical pgvector topology is intentionally not fixed by this ADR. Valid deployments may include:

- one shared cluster with strongly filtered logical namespaces
- separate code and document clusters
- initiative/team-specific clusters
- multiple tables or partitions by workload/source type

Agents and Initiative Profiles must not depend on the chosen physical topology.

## Logical-to-physical resource binding

Initiative Profiles describe **logical resources** such as Jira project scopes, repositories, and knowledge source IDs.

Environment-specific resource bindings resolve those logical resources to physical infrastructure.

Examples:

- logical knowledge source `pos-code` → pgvector connection alias + table/namespace + metadata filter
- logical knowledge source `design-documents` → another table/namespace in the same cluster
- the same logical source → a different cluster in another environment
- logical Jira integration → approved Jira connection/site while the Initiative Profile supplies project scopes

Physical connection strings, credentials, RDS endpoints, table names, schemas, and secret values do not belong in the Initiative Profile.

This indirection allows the organization to change from per-team pgvector instances to shared company-wide code/document instances without modifying agents or initiative-owned business configuration.

## Why

This split aligns each workload with its natural access pattern:

- DynamoDB fits key-oriented, resumable, high-churn operational state.
- S3 fits durable immutable/larger artifacts.
- pgvector fits semantic retrieval and vector search.
- logical resource bindings preserve initiative and environment portability.

It also prevents the knowledge database from becoming an accidental system of record for unrelated workflow state.

## Consequences

The platform operates multiple persistence technologies, but each has a bounded responsibility.

Shared agent code must use platform services/ports rather than direct DynamoDB, S3, or pgvector access.

Future implementation stories must define:

- DynamoDB partition/sort-key access patterns and conditional-write rules
- artifact naming/versioning and S3 lifecycle controls
- pgvector physical topology, metadata model, HNSW/index strategy, and isolation
- resource-binding configuration and secret resolution
- backup, retention, observability, and environment separation

The number and topology of pgvector instances remain an infrastructure decision behind the resource-binding layer, not an agent or Initiative Profile concern.
