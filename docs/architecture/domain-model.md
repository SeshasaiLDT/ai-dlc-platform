# AI-DLC Domain Model

Status: Draft  
Jira: AIDLC-14

## Core concepts

### Initiative
A registered software-delivery or operational domain using AI-DLC.

Owns logical configuration for:
- tool scopes
- repositories
- knowledge sources
- build/test profiles
- policies
- memberships
- defaults

An Initiative does not own physical database endpoints, credentials, pgvector table names, or environment-specific infrastructure locations.

### Membership
The relationship between an authenticated principal and an Initiative.

Defines which initiatives a user can enter and may further constrain:
- roles
- capabilities
- tool permissions
- approved Jira/repository scopes

A user may belong to multiple initiatives. Authentication identity must not be hardcoded directly to one Jira board or one knowledge database.

### Resource Binding
An environment-specific mapping from an Initiative's logical resource to physical infrastructure.

Examples:
- logical knowledge source → pgvector connection alias/table/namespace/filter
- logical Jira integration → approved Jira site/connection
- logical artifact store → S3 bucket/prefix

Bindings contain no agent behavior. They allow infrastructure topology to change without modifying Initiative Profiles or capability-agent code.

### Workspace
A persistent user working context within one initiative.

Contains:
- conversation history
- tasks
- artifacts
- approvals
- selected tickets/repositories
- execution history
- pinned initiative configuration revision

### Task
A durable unit of requested work.

Examples:
- investigate an incident
- analyze change impact
- analyze code impact
- implement a change
- verify a change

### Plan
A bounded ordered or dependency-based set of task steps produced by the orchestrator.

### Agent
An independently deployable runtime that provides one or more capabilities and communicates through defined contracts.

### Capability
A reusable business/engineering function exposed by an agent.

Initial capabilities:
- Investigation
- Change Impact
- Code Analysis
- Implementation
- Verification

### Tool
A governed operation available to an agent.

### Model Role
A logical class of LLM responsibility.

Initial roles:
- Routing
- Standard Reasoning
- Deep Reasoning
- Independent Reviewer

### Policy
A machine-enforced rule controlling access or behavior.

### Approval
A durable human decision required before a guarded operation may proceed.

### Artifact
A generated or discovered durable output.

Artifact bodies may live in object storage while metadata/provenance lives in operational state.

### Evidence
Source material supporting a finding or decision.

### Knowledge Source
An approved logical source available for retrieval.

A Knowledge Source identifies what an initiative may retrieve from. A Resource Binding determines where that source physically resides.

### Repository
A source-code repository registered to an initiative.

### Identity / Role
The authenticated user and their authorization context.
