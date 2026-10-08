# Model Roles

Model roles (`ai_dlc.application.agent_harness.model_roles`, interface 1.5.0) let agents say *what kind of model* they need without naming a model. Concrete deployments are configuration owned by later Epic 6 components (registry, routing, fallback); none of those exist yet.

## Terminology

| Term | Meaning |
| --- | --- |
| **Model role** | A logical need: `routing`, `standard_reasoning`, `deep_reasoning`, `independent_reviewer`. Stable and agent-agnostic. |
| **Model capability** | What a specific deployment supports. Today this is `ModelCapability` (context/output limits used by the budgeter). |
| **Model deployment** | A concrete, configured model endpoint (provider, region, name). Never appears in agent code or in this module. |
| **Agent capability** | What an *agent* can do (A2A agent card). Unrelated to model roles. |

## Roles and default profiles

Defaults are configurable starting points, not claims about any model.

| Role | Intent | Defaults |
| --- | --- | --- |
| `routing` | classification, low latency/cost | structured output, basic reasoning, 8k ctx, lowest latency (≤5 s), high cost sensitivity |
| `standard_reasoning` | general analysis | structured output, moderate reasoning, 32k ctx, balanced latency/cost |
| `deep_reasoning` | hard code/architecture work | structured output + tools, extended reasoning, 128k ctx, relaxed latency, low cost sensitivity |
| `independent_reviewer` | verification, structured findings | structured output, extended reasoning, 64k ctx, `require_separate_from_generator` |

## Requirement contract

`ModelRequirements` = `role` + five typed, frozen sections (unknown fields are rejected):

- **`CapabilityRequirements`**: `structured_output`, `tool_calling`, `reasoning` (`none`..`extended`), `input_modalities`, `response_capabilities` (streaming, citations).
- **`ContextRequirements`**: `min_context_tokens`, `min_output_tokens`; `is_satisfied_by(ModelCapability)` compares against the existing budgeting contract.
- **`LatencyRequirements`**: `preference`, optional `max_latency_ms`, `deadline_required`.
- **`CostRequirements`**: `sensitivity`, optional per-million-token input/output ceilings (`Decimal`, ISO-style `currency`), `require_pricing_metadata` (ceilings require it).
- **`DataGovernance`**: `allowed_classifications` (public/internal/confidential/restricted), `allowed_deployment_types`, allow/deny lists for providers and regions. Empty allow-lists mean "no restriction on that axis"; an item cannot be both allowed and denied. Identifiers are validated plain tokens.

`RoleProfiles` holds exactly one `ModelRequirements` per role and is loaded from configuration with `RoleProfiles.model_validate`. `RoleProfiles.defaults()` supplies the defaults.

## Selection request

`ModelSelectionRequest` is the minimal input for future routing/registry code: role, optional extra capabilities, requested context/output tokens, data classification, optional latency/cost ceilings, provider/region restrictions, and deployments the reviewer must differ from. `effective_requirements(profile)` merges a request into a profile **tightening only** (it can raise minimums, lower ceilings, narrow allow-lists, add denials) and raises if the request cannot be met, e.g. a classification the role does not permit. It does not choose a model.

## Relationship to ModelProvider

The existing `ModelProvider.invoke_model(role: str, ...)` is unchanged. `ModelRole` is a `StrEnum`, so `ModelRole.ROUTING` is passed as the existing `role` argument; there is no second provider interface. Which deployment serves a role is the provider adapter's configuration.

## Configuration example (vendor-neutral)

```python
config = {
    "profiles": {
        "routing": {
            "role": "routing",
            "capabilities": {"structured_output": True, "reasoning": 1},
            "context": {"min_context_tokens": 8000, "min_output_tokens": 512},
            "latency": {"preference": "lowest", "max_latency_ms": 3000},
            "cost": {"sensitivity": "high", "max_output_cost_per_million_tokens": "2.00"},
            "governance": {
                "allowed_classifications": ["public", "internal"],
                "allowed_regions": ["region-a"],
            },
        },
        # ... the other three roles
    }
}
profiles = RoleProfiles.model_validate(config)
```

## Future registry integration

The registry will map deployments (with their `ModelCapability`, pricing, provider, region, deployment type) to the roles they may serve, and the router will filter by `effective_requirements`. Fallback and cost tracking are separate stories.

## Security considerations

- Roles grant nothing. `DEEP_REASONING` gets no additional Jira, Git, ServiceNow, knowledge, tool or initiative access; authorization stays with `AgentContext` and the Gateway.
- Role selection never reads or modifies `AgentContext`; requirement models carry no identity, scope or permission fields.
- Requirements hold no model names, ARNs or secrets. Provider/region eligibility is a governance decision made in configuration.
- Requests can only narrow a profile's governance, never widen it.
