# AI-DLC Enterprise Architecture Principles

Status: Draft  
Owner: AI-DLC  
Jira: AIDLC-13

## Purpose

These principles constrain all future AI-DLC design and implementation decisions. They exist specifically to prevent initiative-specific code from becoming platform architecture.

## Principles

1. **Initiative agnostic by default**  
   Shared platform code must not contain project names, repository names, Jira project keys, ServiceNow groups, business-flow names, team names, or other initiative-specific constants.

2. **Configuration over branching**  
   Initiative differences belong in versioned configuration, registered tools, knowledge, policies, build profiles, or skills. Shared code must not grow `if initiative == ...` branches.

3. **Capability agents, not project agents**  
   Agents represent reusable capabilities such as Investigation, Change Impact, Code Analysis, Implementation, and Verification.

4. **Independent agents communicate through A2A**  
   A2A is used only across true independent agent boundaries. Internal helper functions and local workflow steps do not become agents merely to use A2A.

5. **MCP is a governed tool boundary, not a universal transport**  
   MCP is preferred for enterprise tool access such as Jira and ServiceNow when it improves governance and interoperability. Local filesystem, Git, build, and test operations may execute locally inside isolated workspaces when appropriate.

6. **Shared runtime behavior belongs in the Agent Harness**  
   Model access, A2A, MCP, context propagation, retries, telemetry, structured output, approvals, policy checks, and lifecycle mechanics must not be reimplemented independently by each agent.

7. **Model roles are stable; model names are not**  
   Agents depend on logical roles such as Routing, Standard Reasoning, Deep Reasoning, and Independent Reviewer. Concrete model/provider selection is configuration.

8. **Deterministic routing comes before LLM routing**  
   If user intent is explicit, the platform routes deterministically. A classifier LLM is used only when intent cannot be resolved safely through deterministic rules.

9. **Authorization is enforced outside model reasoning**  
   An LLM may recommend an action but cannot grant itself permission. Identity, authorization, initiative membership, tool permissions, and approval requirements are enforced by platform code.

10. **Writes are higher risk than reads**  
    Read, write, and destructive operations are separately permissioned. Sensitive writes may require human approval.

11. **All significant actions are traceable**  
    A request must be traceable from user → initiative → workspace → task → agent → model/tool → artifact/result.

12. **Agent output is contract-first**  
    Agent inputs, outputs, artifacts, errors, and evidence use versioned schemas rather than free-form assumptions between components.

13. **Long-running work is resumable**  
    Task progress must not depend solely on process memory. Long-running workflows persist execution state and support safe retry/resume.

14. **Generation and verification are independent**  
    The system that generates an artifact does not unilaterally approve it. Verification uses independent policy and, where configured, a separate reviewer model.

15. **Deployment boundaries do not dictate repository boundaries**  
    Multiple AgentCore runtimes may be deployed independently from one backend repository.

16. **Measure before adding infrastructure complexity**  
    Additional databases, queues, runtimes, repositories, or orchestration systems require a demonstrated workload or operational need.

17. **A new initiative must not require shared-source changes**  
    This is the primary architecture acceptance test. If onboarding a valid new initiative requires changing the shared orchestrator, harness, or reusable capability agents, the design is still coupled.
