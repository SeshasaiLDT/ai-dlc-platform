# Environment and Promotion Strategy

Status: Draft  
Jira: AIDLC-17

## Goal

Provide enough environment separation to develop, test, demonstrate, and safely operate AI-DLC without creating unnecessary personal-cloud cost or deployment complexity.

## Environments

### 1. Local

Purpose:
- rapid development
- unit testing
- contract testing with mocks/fakes
- schema and migration development
- local UI/backend integration where practical

Characteristics:
- no production credentials
- local or containerized PostgreSQL may be used for development
- mocked or sandbox integrations preferred
- model/tool adapters should support test doubles
- secrets are loaded only from developer-local secret mechanisms and never committed

Local development must not be required to reach live Jira, ServiceNow, GitHub, or AWS resources for ordinary unit tests.

### 2. AWS Dev

Purpose:
- first real cloud integration environment
- AgentCore runtime validation
- Bedrock model integration
- MCP/A2A integration
- RDS PostgreSQL + pgvector validation
- GitHub/Jira integration testing
- end-to-end workflow development

Characteristics:
- isolated development resources
- non-production credentials and scopes
- synthetic/test initiatives
- disposable test workspaces
- verbose observability enabled
- lower-cost resource sizing

This is the primary environment used while implementing Jira backlog stories.

### 3. AWS Demo

Purpose:
- stable portfolio demonstration environment
- resume/interview demos
- final integration testing before showcasing changes
- realistic end-to-end use with curated sample initiatives

Characteristics:
- more stable than Dev
- controlled deployments only
- curated data
- stricter write permissions
- reduced debug exposure
- persistent demo initiative configuration
- monitored for failures and cost

The Demo environment is not treated as a real enterprise production environment. It is the highest environment required for the personal implementation.

## Promotion Flow

```
Feature Branch
    ↓
Pull Request
    ↓
Automated Validation
    ↓
main
    ↓
AWS Dev
    ↓
E2E / Evaluation / Manual Validation
    ↓
Versioned Release
    ↓
AWS Demo
```

## Branching Strategy

Use trunk-based development with short-lived branches.

Recommended branch format:

```
AIDLC-<ticket>-short-description
```

Examples:

```
AIDLC-19-initiative-schema
AIDLC-38-agent-harness-contract
AIDLC-68-investigation-agent
```

Rules:
- `main` is always releasable.
- No direct implementation commits to `main` once CI is established.
- Branches should normally represent one Jira story.
- Large stories should be decomposed rather than allowing long-lived branches.
- Pull requests must reference the Jira ticket.

## Promotion Requirements

### Local → AWS Dev

Required:
- story meets Definition of Ready before implementation
- lint/type checks pass
- unit tests pass
- contract/schema tests pass
- no committed secrets
- infrastructure changes are reviewed
- migrations are forward-safe

### AWS Dev → AWS Demo

Required:
- story meets Definition of Done
- integration tests pass
- relevant agent evaluation cases pass
- security-sensitive changes have been reviewed
- no unresolved critical/high-risk defects
- rollback path exists
- deployment is versioned/tagged

## Configuration Separation

Environment-specific values must not be embedded in source code.

Examples:
- database endpoints
- AWS resource identifiers
- model selections
- tool endpoints
- credentials
- callback URLs

Use configuration and AWS secret/parameter management.

Initiative configuration and environment configuration are separate concepts.

Example:

```
Environment: dev
Initiative: travel-platform
```

must not be represented as a single hardcoded profile.

## Database Strategy

Each AWS environment receives its own logical persistence boundary.

Initial approach:

```
AWS Dev
  └── PostgreSQL + pgvector

AWS Demo
  └── PostgreSQL + pgvector
```

The implementation may use separate RDS instances/databases depending on cost and isolation requirements, but Dev and Demo data must never share authoritative tables.

## Infrastructure as Code

Cloud resources must be reproducible from source-controlled infrastructure definitions.

Manual console configuration is allowed only during exploration. Any setting required for a repeatable environment must eventually be codified.

## Rollback

Every deployment mechanism must provide a documented way to return to the prior working application/runtime version.

Database migrations must be designed so application rollback does not silently corrupt or orphan platform data.

## Revisit Triggers

Add additional environments only if there is a demonstrated need, such as:
- multiple developers
- dedicated QA ownership
- enterprise integration certification
- customer-facing production usage
- destructive testing incompatible with Dev
- formal release governance

Do not add environments simply to mirror a large-enterprise SDLC diagram.
