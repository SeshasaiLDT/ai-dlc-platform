"""AIDLC-20 application contract tests; no live integrations are required."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.application.initiatives import (
    InitiativeAlreadyExistsError,
    InitiativeIdentityMismatchError,
    InitiativeNotFoundError,
    InitiativeRegistry,
    InitiativeStatus,
    RegisteredInitiative,
    RegistryEventType,
)
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"
FIRST_INSTANT = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.current = FIRST_INSTANT

    def __call__(self) -> datetime:
        return self.current

    def advance(self) -> None:
        self.current += timedelta(minutes=1)


@pytest.fixture
def travel() -> InitiativeProfile:
    return load_initiative_profile(EXAMPLES / "travel-platform.yaml")


@pytest.fixture
def operations() -> InitiativeProfile:
    return load_initiative_profile(EXAMPLES / "field-operations.yaml")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def event_sink() -> InMemoryRegistryEventSink:
    return InMemoryRegistryEventSink()


@pytest.fixture
def registry(clock: FakeClock, event_sink: InMemoryRegistryEventSink) -> InitiativeRegistry:
    return InitiativeRegistry(InMemoryInitiativeRepository(), event_sink, clock=clock)


def updated_name(profile: InitiativeProfile, name: str) -> InitiativeProfile:
    document = profile.model_dump(mode="json")
    document["initiative"]["name"] = name
    return InitiativeProfile.model_validate(document)


def test_create_returns_active_registration_with_stable_id_and_utc_time(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    registered = registry.create(travel)
    assert isinstance(registered, RegisteredInitiative)
    assert registered.id == travel.initiative.id
    assert registered.profile is travel
    assert registered.status == InitiativeStatus.ACTIVE
    assert registered.created_at == FIRST_INSTANT
    assert registered.updated_at == FIRST_INSTANT
    assert registered.created_at.tzinfo == UTC


def test_duplicate_id_rejected_without_second_event(
    registry: InitiativeRegistry,
    event_sink: InMemoryRegistryEventSink,
    travel: InitiativeProfile,
) -> None:
    registry.create(travel)
    with pytest.raises(InitiativeAlreadyExistsError, match="travel-platform"):
        registry.create(updated_name(travel, "Another Name"))
    assert len(event_sink.events) == 1
    assert registry.get(travel.initiative.id).profile is travel


def test_get_by_id_and_missing_error(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    created = registry.create(travel)
    assert registry.get("travel-platform") == created
    with pytest.raises(InitiativeNotFoundError, match="unknown"):
        registry.get("unknown")


def test_list_is_sorted_and_filterable_including_disabled(
    registry: InitiativeRegistry, travel: InitiativeProfile, operations: InitiativeProfile
) -> None:
    registry.create(travel)
    registry.create(operations)
    assert [item.id for item in registry.list()] == ["field-operations", "travel-platform"]
    registry.disable("field-operations")
    assert [item.id for item in registry.list()] == ["field-operations", "travel-platform"]
    assert [item.id for item in registry.list(status=InitiativeStatus.ACTIVE)] == [
        "travel-platform"
    ]
    assert [item.id for item in registry.list(status=InitiativeStatus.DISABLED)] == [
        "field-operations"
    ]


def test_update_replaces_profile_preserves_id_status_and_created_at(
    registry: InitiativeRegistry, clock: FakeClock, travel: InitiativeProfile
) -> None:
    original = registry.create(travel)
    replacement = updated_name(travel, "Travel Services")
    clock.advance()
    updated = registry.update(original.id, replacement)
    assert updated.id == original.id
    assert updated.profile is replacement
    assert updated.profile.initiative.name == "Travel Services"
    assert updated.status == InitiativeStatus.ACTIVE
    assert updated.created_at == original.created_at
    assert updated.updated_at == FIRST_INSTANT + timedelta(minutes=1)
    assert registry.get(original.id) == updated
    assert original.profile is travel


def test_update_cannot_change_stable_identity(
    registry: InitiativeRegistry,
    event_sink: InMemoryRegistryEventSink,
    travel: InitiativeProfile,
    operations: InitiativeProfile,
) -> None:
    original = registry.create(travel)
    with pytest.raises(InitiativeIdentityMismatchError, match="field-operations"):
        registry.update(original.id, operations)
    assert registry.get(original.id) == original
    assert len(event_sink.events) == 1


def test_update_missing_is_explicit(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    with pytest.raises(InitiativeNotFoundError, match="travel-platform"):
        registry.update("travel-platform", travel)


def test_disable_retains_profile_and_is_idempotent(
    registry: InitiativeRegistry,
    event_sink: InMemoryRegistryEventSink,
    clock: FakeClock,
    travel: InitiativeProfile,
) -> None:
    original = registry.create(travel)
    clock.advance()
    disabled = registry.disable(original.id)
    assert disabled.status == InitiativeStatus.DISABLED
    assert disabled.profile is travel
    assert disabled.created_at == original.created_at
    assert disabled.updated_at == FIRST_INSTANT + timedelta(minutes=1)
    assert registry.get(original.id) == disabled
    clock.advance()
    assert registry.disable(original.id) is disabled
    assert registry.get(original.id).profile is travel
    assert len(event_sink.events) == 2


def test_disable_missing_is_explicit(registry: InitiativeRegistry) -> None:
    with pytest.raises(InitiativeNotFoundError, match="unknown"):
        registry.disable("unknown")


def test_update_disabled_does_not_reactivate(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    registry.create(travel)
    registry.disable(travel.initiative.id)
    updated = registry.update(travel.initiative.id, updated_name(travel, "Travel Services"))
    assert updated.status == InitiativeStatus.DISABLED
    assert registry.get(travel.initiative.id).status == InitiativeStatus.DISABLED


def test_mutation_events_have_type_id_time_and_operation_metadata(
    registry: InitiativeRegistry,
    event_sink: InMemoryRegistryEventSink,
    clock: FakeClock,
    travel: InitiativeProfile,
) -> None:
    registry.create(travel)
    clock.advance()
    registry.update(travel.initiative.id, updated_name(travel, "Travel Services"))
    clock.advance()
    registry.disable(travel.initiative.id)
    events = event_sink.events
    assert [event.event_type for event in events] == [
        RegistryEventType.INITIATIVE_CREATED,
        RegistryEventType.INITIATIVE_UPDATED,
        RegistryEventType.INITIATIVE_DISABLED,
    ]
    assert all(event.initiative_id == travel.initiative.id for event in events)
    assert [event.occurred_at for event in events] == [
        FIRST_INSTANT,
        FIRST_INSTANT + timedelta(minutes=1),
        FIRST_INSTANT + timedelta(minutes=2),
    ]
    assert events[0].metadata == {
        "schema_version": "1.0",
        "status_before": "unregistered",
        "status_after": "active",
        "revision": "1",
    }
    assert events[1].metadata["status_before"] == "active"
    assert events[1].metadata["revision"] == "2"
    assert events[1].metadata["prior_revision"] == "1"
    assert events[2].metadata["status_after"] == "disabled"
    assert events[2].metadata["revision"] == "2"
    with pytest.raises(TypeError):
        events[0].metadata["status_after"] = "changed"


def test_reads_do_not_emit_events(
    registry: InitiativeRegistry,
    event_sink: InMemoryRegistryEventSink,
    travel: InitiativeProfile,
) -> None:
    registry.create(travel)
    event_count = len(event_sink.events)
    registry.get(travel.initiative.id)
    registry.list()
    registry.list(status=InitiativeStatus.ACTIVE)
    assert len(event_sink.events) == event_count


def test_both_distinct_profiles_register_without_source_changes(
    registry: InitiativeRegistry, travel: InitiativeProfile, operations: InitiativeProfile
) -> None:
    registered = (registry.create(travel), registry.create(operations))
    assert {item.id for item in registered} == {"travel-platform", "field-operations"}
    assert registered[0].profile.integrations.jira.enabled
    assert registered[1].profile.integrations.servicenow.enabled


def test_returned_records_do_not_expose_mutable_storage(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    record = registry.create(travel)
    with pytest.raises(FrozenInstanceError):
        record.status = InitiativeStatus.DISABLED
    with pytest.raises(ValidationError, match="frozen"):
        record.profile.initiative.name = "Changed"
    assert registry.get(record.id).status == InitiativeStatus.ACTIVE
    assert registry.list()[0].profile.initiative.name == "Travel Platform"


def test_clock_is_normalized_to_utc(travel: InitiativeProfile) -> None:
    eastern = timezone(timedelta(hours=-5))
    local_time = datetime(2026, 1, 1, 22, 4, tzinfo=eastern)
    registry = InitiativeRegistry(
        InMemoryInitiativeRepository(), InMemoryRegistryEventSink(), clock=lambda: local_time
    )
    assert registry.create(travel).created_at == FIRST_INSTANT


def test_naive_clock_is_rejected_before_mutation(travel: InitiativeProfile) -> None:
    repository = InMemoryInitiativeRepository()
    sink = InMemoryRegistryEventSink()
    registry = InitiativeRegistry(repository, sink, clock=lambda: datetime(2026, 1, 2))
    with pytest.raises(ValueError, match="timezone-aware"):
        registry.create(travel)
    assert registry.list() == ()
    assert sink.events == ()
