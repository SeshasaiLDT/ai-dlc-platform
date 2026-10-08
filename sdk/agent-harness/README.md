# ai-dlc-agent-harness

Shared Agent Harness SDK for independently deployed AI-DLC agents: lifecycle and trusted
`AgentContext`, governed MCP tool client, A2A client/server layer, structured output
validation, context/token budgeting, and retry/idempotency primitives.

```bash
pip install "ai-dlc-agent-harness==0.1.0"           # core
pip install "ai-dlc-agent-harness[a2a]==0.1.0"      # + A2A layer
```

```python
from ai_dlc.application.agent_harness import INTERFACE_VERSION, LifecycleRunner

assert INTERFACE_VERSION.startswith("1.")  # pin the interface major you were tested against
```

Import path, versions, dependency groups, the reference agent, AgentCore integration,
publishing, and upgrade/rollback procedures are documented in
`docs/architecture/agent-harness-sdk.md` in the platform repository.
