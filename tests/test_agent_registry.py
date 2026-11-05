"""Manifest validation, versioning, discovery and extensible registration contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ai_dlc.adapters.agent_registry.in_memory import InMemoryAgentRegistryRepository
from ai_dlc.application.agent_registry import AgentHealth, AgentManifest, AgentRegistry

NOW = datetime(2026, 11, 5, tzinfo=UTC)


def manifest(agent_id="reviewer", **changes):
    values = dict(
        agent_id=agent_id,
        name="Reviewer",
        description="Reviews documents",
        agent_version="1.2.0",
        a2a_endpoint=f"https://{agent_id}.example.invalid/a2a",
        capabilities=("document.review",),
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        output_schema={"type": "object", "properties": {"verdict": {"type": "string"}}},
        permissions=frozenset({"documents.read"}),
    )
    values.update(changes)
    return AgentManifest(**values)


def registry():
    return AgentRegistry(InMemoryAgentRegistryRepository(), clock=lambda: NOW)


def test_manifest_requires_valid_semver_endpoint_and_json_schemas():
    with pytest.raises(ValidationError):
        manifest(manifest_version="v1")
    with pytest.raises(ValidationError):
        manifest(a2a_endpoint="file:///tmp/agent")
    with pytest.raises(ValidationError):
        manifest(input_schema={"type": "made-up"})
    with pytest.raises(ValidationError):
        manifest(output_schema={"$ref": "https://example.invalid/schema.json"})


def test_registration_versions_revisions_and_preserves_manifest_version():
    agents = registry()
    first = agents.register(manifest())
    revised = agents.register(manifest(description="Updated reviewer"))
    assert (first.revision, revised.revision) == (1, 2)
    assert revised.manifest.manifest_version == "1.0.0"
    assert revised.manifest.agent_version == "1.2.0"


def test_discovery_filters_capabilities_permissions_and_health():
    agents = registry()
    agents.register(manifest())
    agents.register(
        manifest(
            "writer",
            capabilities=("document.write",),
            permissions=frozenset({"documents.write"}),
            health=AgentHealth(status="unhealthy"),
        )
    )
    found = agents.discover(
        required_capabilities=frozenset({"document.review"}),
        allowed_permissions=frozenset({"documents.read"}),
    )
    assert [item.manifest.agent_id for item in found] == ["reviewer"]
    unhealthy = agents.discover(
        include_unhealthy=True,
        allowed_permissions=frozenset({"documents.write"}),
    )
    assert unhealthy[0].manifest.agent_id == "writer"


def test_new_agent_is_discovered_without_orchestrator_catalog_changes():
    agents = registry()
    agents.register(manifest("first"))
    agents.register(manifest("new-specialist", capabilities=("image.inspect",)))
    assert {
        record.manifest.agent_id
        for record in agents.discover(allowed_permissions=frozenset({"documents.read"}))
    } == {
        "first",
        "new-specialist",
    }
