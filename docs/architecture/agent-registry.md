# Agent registry and capability manifest (AIDLC-53)

`ai_dlc.application.agent_registry.AgentManifest` is the versioned discovery
contract for independently deployed agents. `manifest_version` versions the
contract shape (SemVer); `agent_version` versions the agent implementation.
Registry `revision` versions each stored update. The manifest carries stable
identity, human-readable metadata, capability names, an A2A HTTP(S) endpoint,
Draft 2020-12 input and output JSON Schemas, permission labels, and health
metadata. Schemas are checked at registration and external `$ref` values are
rejected.

`AgentRegistryReader.discover` gives a caller a deterministic snapshot filtered
by requested capabilities, the caller's allowed permission labels, enabled
state, and health. Permission labels are descriptive requirements; a manifest
does not grant access. The caller must supply trusted policy-derived allowed
permissions. The current `InMemoryAgentRegistryRepository` is for local use and
tests; deployments need a shared repository adapter implementing the same port
before separately running orchestrators and agents can see common registrations.

The existing A2A harness remains responsible for protocol cards and transport.
The registry does not deploy agents, authenticate transport, perform health
probes, or implement orchestrator routing. AIDLC-54 owns the enterprise runtime.
To add an agent, register a manifest through `AgentRegistry`; consumers discover
it by ID and capability without maintaining an orchestrator-side agent catalog.
