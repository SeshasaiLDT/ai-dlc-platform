# AWS Deployment Topology

Status: Draft

## Purpose

Define preferred AWS service boundaries for AI-DLC without prematurely assigning every workload to a container, queue, or database.

## Experience layer

The React/Vite UI should default to:

```text
Route 53
  -> ACM
  -> CloudFront
  -> S3 static assets
```

The SPA does not require EC2 or ECS/Fargate merely to serve frontend code. WAF may be added at the public boundary when enterprise security policy requires it.

## Control plane and API

Preferred initial topology:

```text
UI
  -> API Gateway
  -> Lambda
  -> AI-DLC control-plane services
```

Lambda is the preferred starting point for stateless API/control-plane workloads.

ECS/Fargate remains an allowed alternative when measured needs justify persistent container execution, sustained connections, long-running background workers, or other workloads that do not fit Lambda or AgentCore well.

## Agent compute

Independent capability agents run in Bedrock AgentCore Runtime.

Agent container images are stored in ECR.

AgentCore Gateway is used for governed MCP/tool access where appropriate.

Do not duplicate agent execution onto ECS/Fargate unless a future workload explicitly requires another compute boundary.

## Model layer

Amazon Bedrock provides model inference.

Agents depend on logical model roles rather than concrete model names or providers.

## Operational state

DynamoDB is the preferred operational system of record for:
- initiative records and revision metadata
- memberships and authorization-supporting mappings
- sessions and workspaces
- tasks, plans, checkpoints, and resumable state
- approvals
- agent execution state
- resource bindings
- artifact metadata/provenance where key-oriented access fits

## Artifact storage

Amazon S3 stores canonical larger or immutable artifacts such as:
- approved design artifacts
- implementation/change artifacts
- verification reports
- evidence bundles
- generated/exported files

Agents access artifacts through platform contracts instead of hardcoded bucket names or prefixes.

## Knowledge retrieval

Amazon RDS for PostgreSQL with pgvector provides semantic knowledge retrieval.

It stores:
- code/document chunks
- embeddings
- retrieval metadata and provenance
- HNSW/vector indexes

Physical pgvector topology is resolved through Resource Bindings. Valid layouts include:
- shared database instances with multiple tables/namespaces
- separate code and document instances
- initiative/team-specific instances
- hybrid layouts

Agents never select database instances, tables, schemas, or indexes directly.

## Asynchronous workloads

SQS and EventBridge are optional and workload-driven.

Potential SQS use cases include ingestion, embedding/indexing jobs, repository synchronization, notifications, and retryable background processing.

Potential EventBridge use cases include lifecycle events such as InitiativeOnboarded, DesignApproved, ImplementationApproved, VerificationCompleted, and KnowledgeSourceUpdated.

Do not add asynchronous infrastructure where synchronous execution remains simpler.

## Networking

The deployment design must define:
- VPC and subnet boundaries
- private RDS connectivity
- security groups
- controlled outbound access for approved Git/Jira/ServiceNow endpoints
- NAT or another approved egress path where necessary
- VPC endpoints where useful for AWS services
- DNS/TLS boundaries
- least-privilege paths between control plane, agents, and data stores

Networking is a first-class concern because restricted egress can prevent agents from reaching approved external systems.

## Security and secrets

Use:
- IAM for workload identity and least privilege
- Secrets Manager for credentials and connection secrets
- KMS where customer-managed encryption keys are required
- ACM for public TLS
- WAF where required for public HTTP protection

Initiative Profiles and Resource Bindings contain aliases/routing metadata, not secret values.

## Observability and audit

Use CloudWatch for logs, metrics, and alarms.

Use CloudTrail for AWS control-plane audit.

Maintain a separate application-level audit trail for user -> initiative -> task -> agent -> tool/model -> artifact/result traceability.

## Service classification

Core expected services:
- Bedrock
- AgentCore Runtime
- AgentCore Gateway where applicable
- ECR
- DynamoDB
- S3
- RDS PostgreSQL + pgvector
- CloudFront
- API Gateway
- Lambda
- IAM
- Secrets Manager
- CloudWatch
- VPC/network controls

Likely supporting services:
- Route 53
- ACM
- KMS
- CloudTrail

Conditional services:
- ECS/Fargate
- SQS
- EventBridge
- WAF

Additional infrastructure must be justified by a concrete workload, security requirement, or operational need.
