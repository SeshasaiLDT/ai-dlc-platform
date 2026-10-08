"""Binding existence/routing must never replace logical authorization."""

from dataclasses import replace

import pytest
from support.enterprise import RESOURCE_TYPES, SCOPE_FIELDS

from ai_dlc.application.authorization import ScopeRestriction
from ai_dlc.application.resource_bindings import ResourceBindingKey
from ai_dlc.domain.initiative.enums import GitProvider

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "failure", ["missing", "disabled", "wrong_environment", "wrong_initiative"]
)
def test_binding_failures_normalize_without_provider_execution(world_factory, domain, failure):
    world = world_factory(omit=((domain, 0),))
    key = world.key(domain)
    if failure != "missing":
        registered_key = {
            "disabled": key,
            "wrong_environment": replace(key, environment="stage"),
            "wrong_initiative": replace(key, initiative_id="initiative-unrelated"),
        }[failure]
        binding = world.bindings.register(
            registered_key, world.details(domain), correlation_id="setup-failure"
        )
        if failure == "disabled":
            world.bindings.disable(
                registered_key, expected_revision=binding.revision, correlation_id="disable"
            )
    result = world.call(domain)
    assert result["outcome"] == "error"
    assert result["error"]["code"] == "UPSTREAM_AUTH_CONFIGURATION"
    assert result["error"]["retryable"] is False
    assert "managed-" not in str(result)
    assert "physical-" not in str(result)
    assert not world.calls()


def test_repository_returning_another_initiative_binding_fails_closed(world, domain, monkeypatch):
    foreign = world.bindings.get(world.key(domain, 1))
    monkeypatch.setattr(world.binding_repository, "find", lambda key: foreign)
    result = world.call(domain)
    assert result["error"]["code"] == "UPSTREAM_AUTH_CONFIGURATION"
    assert not world.calls()


def test_narrowed_resource_remains_denied_even_with_an_active_binding(world, domain):
    assert world.bindings.get(world.key(domain))
    context = world.context(
        scope_restriction=ScopeRestriction(**{SCOPE_FIELDS[domain]: frozenset()})
    )
    world.binding_repository.lookups.clear()
    result = world.call(domain, context=context)
    assert result["error"]["code"] == "INVALID_SCOPE"
    assert not world.binding_repository.lookups
    assert not world.calls()


@pytest.mark.parametrize("selected", ["jira", "servicenow"])
def test_jira_and_servicenow_reject_other_logical_connection_ids(selected):
    with pytest.raises(ValueError, match="binding ID"):
        ResourceBindingKey("dev", "initiative-alpha", RESOURCE_TYPES[selected], "unapproved")


def test_git_wrong_logical_binding_and_provider_mismatch_fail_without_adapter_call(world_factory):
    world = world_factory(omit=(("git", 0),))
    wrong_key = replace(world.key("git"), logical_resource_id="repo-other")
    world.bindings.register(wrong_key, world.details("git"), correlation_id="setup")
    assert world.call("git")["error"]["code"] == "UPSTREAM_AUTH_CONFIGURATION"
    world.bindings.register(
        world.key("git"),
        replace(world.details("git"), provider=GitProvider.GITLAB),
        correlation_id="setup",
    )
    assert world.call("git")["error"]["code"] == "UPSTREAM_AUTH_CONFIGURATION"
    assert not world.calls()
