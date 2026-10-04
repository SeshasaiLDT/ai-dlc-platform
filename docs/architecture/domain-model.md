# AI-DLC Domain Model

Status: Draft  
Jira: AIDLC-14

## Core concepts

### Initiative
A registered software-delivery or operational domain using AI-DLC.

Owns:
- tool scopes
- repositories
- knowledge sources
- build/test profiles
- policies
- memberships
- defaults

### Workspace
A persistent user working context within one initiative.

Contains:
- conversation history
- tasks
- artifacts
- approvals
- selected tickets/repositories
- execution history

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

### Evidence
Source material supporting a finding or decision.

### Knowledge Source
An approved source available for retrieval.

### Repository
A source-code repository registered to an initiative.

### Identity / Role
The authenticated user and their authorization context.
