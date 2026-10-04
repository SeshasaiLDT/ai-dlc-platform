# ADR-004: MCP as Governed Enterprise Tool Boundary

Status: Proposed

## Decision
Use MCP where it provides a clean governed boundary for enterprise tools, initially:
- Jira
- ServiceNow

GitHub/Git usage is split:
- remote repository/provider operations may be exposed through governed provider tools or MCP
- local checkout, diff, build, test, and patch operations execute inside an isolated task workspace

## Consequences
MCP is not treated as a universal transport. Tool choice follows the actual trust and execution boundary.
