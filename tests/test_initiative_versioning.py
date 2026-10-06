"""Append-only configuration revisions and Registry rollback semantics."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dlc.adapters.initiatives import InMemoryInitiativeRepository, InMemoryRegistryEventSink
from ai_dlc.application.initiatives import (
    ConfigurationRevision,
    ConfigurationRevisionReason,
    InitiativeNotFoundError,
    InitiativeRegistry,
    InitiativeRevisionConflictError,
    InitiativeRevisionNotFoundError,
    InitiativeStatus,
    RegistryEventType,
)
from ai_dlc.domain.initiative import InitiativeProfile, load_initiative_profile

EXAMPLES = Path(__file__).resolve().parents[1] / "configs" / "initiatives" / "examples"
START = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.current = START

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
def repository() -> InMemoryInitiativeRepository:
    return InMemoryInitiativeRepository()


@pytest.fixture
def sink() -> InMemoryRegistryEventSink:
    return InMemoryRegistryEventSink()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def registry(
    repository: InMemoryInitiativeRepository, sink: InMemoryRegistryEventSink, clock: FakeClock
) -> InitiativeRegistry:
    return InitiativeRegistry(repository, sink, clock=clock)


def renamed(profile: InitiativeProfile, name: str) -> InitiativeProfile:
    document = profile.model_dump(mode="json")
    document["initiative"]["name"] = name
    return InitiativeProfile.model_validate(document)


def revisions(registry: InitiativeRegistry, initiative_id: str) -> list[int]:
    return [item.revision for item in registry.list_revisions(initiative_id)]


def test_create_records_revision_one_and_original_profile(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    created = registry.create(travel)
    first = registry.get_revision(created.id, 1)
    assert created.current_revision == 1
    assert first.initiative_id == created.id
    assert first.profile is travel
    assert first.reason == ConfigurationRevisionReason.CREATED
    assert first.source_revision is None
    assert first.created_at == created.created_at == START
    assert registry.list_revisions(created.id) == (first,)


def test_updates_append_monotonic_revisions_and_preserve_history(
    registry: InitiativeRegistry, clock: FakeClock, travel: InitiativeProfile
) -> None:
    registry.create(travel)
    second_profile = renamed(travel, "Travel Two")
    clock.advance()
    second = registry.update(travel.initiative.id, second_profile)
    clock.advance()
    third = registry.update(travel.initiative.id, renamed(travel, "Travel Three"))
    assert second.current_revision == 2
    assert third.current_revision == 3
    assert revisions(registry, third.id) == [1, 2, 3]
    assert registry.get_revision(third.id, 1).profile is travel
    assert registry.get_revision(third.id, 2).profile is second_profile
    assert registry.get_revision(third.id, 3).profile is third.profile
    assert [item.reason for item in registry.list_revisions(third.id)] == [
        ConfigurationRevisionReason.CREATED,
        ConfigurationRevisionReason.UPDATED,
        ConfigurationRevisionReason.UPDATED,
    ]
    assert [item.created_at for item in registry.list_revisions(third.id)] == [
        START,
        START + timedelta(minutes=1),
        START + timedelta(minutes=2),
    ]


def test_revision_lookup_errors_are_explicit(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    registry.create(travel)
    with pytest.raises(InitiativeRevisionNotFoundError, match="revision 2"):
        registry.get_revision(travel.initiative.id, 2)
    with pytest.raises(InitiativeRevisionNotFoundError, match="revision 0"):
        registry.get_revision(travel.initiative.id, 0)
    with pytest.raises(InitiativeNotFoundError, match="unknown"):
        registry.get_revision("unknown", 1)
    with pytest.raises(InitiativeNotFoundError, match="unknown"):
        registry.list_revisions("unknown")


def test_rollback_from_three_to_one_appends_four_without_erasing_history(
    registry: InitiativeRegistry, clock: FakeClock, travel: InitiativeProfile
) -> None:
    registry.create(travel)
    clock.advance()
    registry.update(travel.initiative.id, renamed(travel, "Travel Two"))
    clock.advance()
    registry.update(travel.initiative.id, renamed(travel, "Travel Three"))
    clock.advance()
    rolled = registry.rollback(travel.initiative.id, 1)
    assert rolled.current_revision == 4
    assert rolled.profile == travel
    assert rolled.profile is registry.get_revision(rolled.id, 1).profile
    assert revisions(registry, rolled.id) == [1, 2, 3, 4]
    assert registry.get_revision(rolled.id, 2).profile.initiative.name == "Travel Two"
    assert registry.get_revision(rolled.id, 3).profile.initiative.name == "Travel Three"
    fourth = registry.get_revision(rolled.id, 4)
    assert fourth.reason == ConfigurationRevisionReason.ROLLBACK
    assert fourth.source_revision == 1
    assert fourth.created_at == rolled.updated_at == START + timedelta(minutes=3)


def test_rollback_current_is_noop_without_timestamp_or_event_change(
    registry: InitiativeRegistry,
    clock: FakeClock,
    sink: InMemoryRegistryEventSink,
    travel: InitiativeProfile,
) -> None:
    original = registry.create(travel)
    clock.advance()
    assert registry.rollback(original.id, 1) is original
    assert revisions(registry, original.id) == [1]
    assert original.updated_at == START
    assert len(sink.events) == 1


def test_rollback_missing_revision_does_not_mutate_or_emit(
    registry: InitiativeRegistry, sink: InMemoryRegistryEventSink, travel: InitiativeProfile
) -> None:
    original = registry.create(travel)
    with pytest.raises(InitiativeRevisionNotFoundError, match="revision 9"):
        registry.rollback(original.id, 9)
    with pytest.raises(InitiativeNotFoundError, match="unknown"):
        registry.rollback("unknown", 1)
    assert registry.get(original.id) is original
    assert revisions(registry, original.id) == [1]
    assert len(sink.events) == 1


def test_rollback_disabled_preserves_status_and_disable_adds_no_revision(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    registry.create(travel)
    registry.update(travel.initiative.id, renamed(travel, "Travel Two"))
    disabled = registry.disable(travel.initiative.id)
    assert disabled.current_revision == 2
    assert revisions(registry, disabled.id) == [1, 2]
    rolled = registry.rollback(disabled.id, 1)
    assert rolled.status == InitiativeStatus.DISABLED
    assert rolled.current_revision == 3
    assert registry.get(rolled.id).status == InitiativeStatus.DISABLED


def test_rollback_after_rollback_and_update_continue_sequence(
    registry: InitiativeRegistry, travel: InitiativeProfile
) -> None:
    registry.create(travel)
    registry.update(travel.initiative.id, renamed(travel, "Travel Two"))
    registry.rollback(travel.initiative.id, 1)
    fourth = registry.rollback(travel.initiative.id, 2)
    fifth = registry.update(travel.initiative.id, renamed(travel, "Travel Five"))
    assert fourth.current_revision == 4
    assert fourth.profile.initiative.name == "Travel Two"
    assert registry.get_revision(fourth.id, 4).source_revision == 2
    assert fifth.current_revision == 5
    assert revisions(registry, fifth.id) == [1, 2, 3, 4, 5]


def test_revision_queries_do_not_emit_events(
    registry: InitiativeRegistry, sink: InMemoryRegistryEventSink, travel: InitiativeProfile
) -> None:
    registry.create(travel)
    count = len(sink.events)
    registry.get(travel.initiative.id)
    registry.get_revision(travel.initiative.id, 1)
    registry.list_revisions(travel.initiative.id)
    assert len(sink.events) == count


def test_events_include_revision_and_rollback_source(
    registry: InitiativeRegistry,
    sink: InMemoryRegistryEventSink,
    clock: FakeClock,
    travel: InitiativeProfile,
) -> None:
    registry.create(travel)
    clock.advance()
    registry.update(travel.initiative.id, renamed(travel, "Travel Two"))
    clock.advance()
    registry.rollback(travel.initiative.id, 1)
    assert [event.event_type for event in sink.events] == [
        RegistryEventType.INITIATIVE_CREATED,
        RegistryEventType.INITIATIVE_UPDATED,
        RegistryEventType.INITIATIVE_ROLLED_BACK,
    ]
    assert sink.events[0].metadata["revision"] == "1"
    assert sink.events[1].metadata["revision"] == "2"
    assert sink.events[1].metadata["prior_revision"] == "1"
    assert sink.events[2].metadata["revision"] == "3"
    assert sink.events[2].metadata["prior_revision"] == "2"
    assert sink.events[2].metadata["source_revision"] == "1"
    assert sink.events[2].occurred_at == registry.get_revision(travel.initiative.id, 3).created_at


def test_historical_snapshots_are_immutable_and_adapter_returns_tuple(
    registry: InitiativeRegistry,
    repository: InMemoryInitiativeRepository,
    travel: InitiativeProfile,
) -> None:
    registry.create(travel)
    snapshot = repository.list_revisions(travel.initiative.id)
    assert type(snapshot) is tuple
    with pytest.raises(FrozenInstanceError):
        snapshot[0].revision = 9
    with pytest.raises(ValidationError, match="frozen"):
        snapshot[0].profile.initiative.name = "Changed"
    registry.update(travel.initiative.id, renamed(travel, "Travel Two"))
    assert len(snapshot) == 1
    assert len(repository.list_revisions(travel.initiative.id)) == 2


def test_two_distinct_examples_have_independent_history(
    registry: InitiativeRegistry, travel: InitiativeProfile, operations: InitiativeProfile
) -> None:
    registry.create(travel)
    registry.create(operations)
    registry.update(travel.initiative.id, renamed(travel, "Travel Two"))
    registry.update(operations.initiative.id, renamed(operations, "Operations Two"))
    assert revisions(registry, travel.initiative.id) == [1, 2]
    assert revisions(registry, operations.initiative.id) == [1, 2]
    assert registry.get_revision(operations.initiative.id, 1).profile is operations
    assert registry.get_revision(travel.initiative.id, 1).profile is travel


def test_revision_time_is_utc_after_offset_clock(travel: InitiativeProfile) -> None:
    offset = timezone(timedelta(hours=-7))
    local_time = START.astimezone(offset)
    registry = InitiativeRegistry(
        InMemoryInitiativeRepository(), InMemoryRegistryEventSink(), clock=lambda: local_time
    )
    registered = registry.create(travel)
    assert registered.created_at == START
    assert registry.get_revision(registered.id, 1).created_at.tzinfo == UTC
    updated = registry.update(registered.id, renamed(travel, "Travel Two"))
    assert updated.updated_at == START
    assert registry.get_revision(registered.id, 2).created_at.tzinfo == UTC


def test_naive_clock_rejected_before_append(travel: InitiativeProfile) -> None:
    repository = InMemoryInitiativeRepository()
    clock = FakeClock()
    registry = InitiativeRegistry(repository, InMemoryRegistryEventSink(), clock=clock)
    registry.create(travel)
    clock.current = datetime(2026, 1, 2)
    with pytest.raises(ValueError, match="timezone-aware"):
        registry.update(travel.initiative.id, renamed(travel, "Travel Two"))
    assert revisions(registry, travel.initiative.id) == [1]


def test_stale_append_rejected_without_duplicate_or_lost_revision(
    registry: InitiativeRegistry,
    repository: InMemoryInitiativeRepository,
    travel: InitiativeProfile,
) -> None:
    stale = registry.create(travel)
    registry.update(stale.id, renamed(travel, "Travel Two"))
    candidate = ConfigurationRevision(
        stale.id,
        2,
        renamed(travel, "Stale"),
        START,
        ConfigurationRevisionReason.UPDATED,
    )
    with pytest.raises(InitiativeRevisionConflictError):
        repository.append_revision(
            replace(stale, profile=candidate.profile, current_revision=2),
            candidate,
            expected_current=stale,
        )
    assert revisions(registry, stale.id) == [1, 2]
    assert registry.get(stale.id).profile.initiative.name == "Travel Two"


def test_direct_replace_cannot_bypass_revision_history(
    registry: InitiativeRegistry,
    repository: InMemoryInitiativeRepository,
    travel: InitiativeProfile,
) -> None:
    current = registry.create(travel)
    with pytest.raises(InitiativeRevisionConflictError):
        repository.replace(replace(current, profile=renamed(travel, "Unversioned")))
    assert registry.get(current.id) is current
    assert revisions(registry, current.id) == [1]


def test_adapter_rejects_rollback_content_not_from_source(
    registry: InitiativeRegistry,
    repository: InMemoryInitiativeRepository,
    travel: InitiativeProfile,
) -> None:
    current = registry.create(travel)
    wrong = renamed(travel, "Different")
    candidate = ConfigurationRevision(
        current.id,
        2,
        wrong,
        START,
        ConfigurationRevisionReason.ROLLBACK,
        source_revision=1,
    )
    with pytest.raises(InitiativeRevisionConflictError):
        repository.append_revision(
            replace(current, profile=wrong, current_revision=2),
            candidate,
            expected_current=current,
        )
    assert revisions(registry, current.id) == [1]


def test_concurrent_updates_only_one_wins_given_same_expected_revision(
    repository: InMemoryInitiativeRepository, travel: InitiativeProfile
) -> None:
    sink = InMemoryRegistryEventSink()
    registry = InitiativeRegistry(repository, sink, clock=lambda: START)
    registry.create(travel)
    current = registry.get(travel.initiative.id)
    candidates = [
        ConfigurationRevision(
            current.id,
            2,
            renamed(travel, name),
            START,
            ConfigurationRevisionReason.UPDATED,
        )
        for name in ("First", "Second")
    ]

    def append(candidate: ConfigurationRevision) -> str:
        try:
            repository.append_revision(
                replace(current, profile=candidate.profile, current_revision=2),
                candidate,
                expected_current=current,
            )
        except InitiativeRevisionConflictError:
            return "conflict"
        return "appended"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(append, candidates))
    assert sorted(outcomes) == ["appended", "conflict"]
    assert revisions(registry, current.id) == [1, 2]


def test_revision_model_rejects_wrong_ownership_and_invalid_rollback_source(
    travel: InitiativeProfile, operations: InitiativeProfile
) -> None:
    with pytest.raises(ValueError, match="ID must match"):
        ConfigurationRevision(
            operations.initiative.id, 1, travel, START, ConfigurationRevisionReason.CREATED
        )
    with pytest.raises(ValueError, match="earlier positive"):
        ConfigurationRevision(
            travel.initiative.id,
            2,
            travel,
            START,
            ConfigurationRevisionReason.ROLLBACK,
            source_revision=2,
        )
    with pytest.raises(ValueError, match="reason"):
        ConfigurationRevision(travel.initiative.id, 2, travel, START, "invalid")
