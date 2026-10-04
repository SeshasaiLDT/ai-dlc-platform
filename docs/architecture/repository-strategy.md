# Repository Strategy

Status: Accepted for initial implementation  
Jira: AIDLC-15

## Decision

Use exactly two source repositories for the initial AI-DLC implementation:

1. `ai-dlc-platform`
2. `ai-dlc-ui`

## ai-dlc-platform

Contains all backend and agent implementation:
- API/control plane
- orchestrator
- capability agents
- shared Agent Harness / SDK
- MCP integrations
- A2A contracts
- persistence
- evaluation framework
- infrastructure-as-code

Independent agents may still deploy to separate AgentCore runtimes.

## ai-dlc-ui

Contains:
- React + TypeScript frontend
- authentication/session UX
- initiative and workspace UX
- guided capability actions
- task progress
- artifact views
- human approvals
- administrative screens

## Why not one repository?

The UI has a different release cycle, dependency graph, build toolchain, and deployable artifact from the backend/agent platform.

## Why not one repository per agent?

Separate agent repositories would add CI/CD, versioning, dependency, testing, and local-development overhead before there is evidence that the organizational benefit outweighs the complexity.

## Revisit trigger

Revisit only if ownership, access control, repository scale, or release independence materially justify more repositories.
