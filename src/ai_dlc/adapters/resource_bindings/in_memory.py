"""Atomic local/test repository with physical-topology isolation checks."""

from threading import Lock

from ai_dlc.application.resource_bindings.errors import (
    BindingConflictError,
    BindingExistsError,
    BindingIsolationError,
    BindingNotFoundError,
)
from ai_dlc.application.resource_bindings.models import (
    ArtifactStoreBinding,
    BindingMutationEvent,
    BindingStatus,
    KnowledgeBinding,
    KnowledgeIsolation,
    ResourceBinding,
    ResourceBindingKey,
)


def _validate_knowledge(binding: ResourceBinding, others: tuple[ResourceBinding, ...]) -> None:
    details = binding.details
    if not isinstance(details, KnowledgeBinding) or binding.status is BindingStatus.DISABLED:
        return
    if any(
        item.field == "initiative_id" and item.value != binding.key.initiative_id
        for item in details.mandatory_filters
    ):
        raise BindingIsolationError("knowledge initiative filter mismatches binding")
    if details.isolation is KnowledgeIsolation.FILTERED_SHARED_LOCATION and not any(
        item.field == "initiative_id" and item.value == binding.key.initiative_id
        for item in details.mandatory_filters
    ):
        raise BindingIsolationError("shared knowledge location requires initiative filter")
    for other in others:
        peer = other.details
        if (
            other.status is BindingStatus.DISABLED
            or other.key.environment != binding.key.environment
            or other.key.initiative_id == binding.key.initiative_id
            or not isinstance(peer, KnowledgeBinding)
            or peer.connection_alias != details.connection_alias
        ):
            continue
        if (
            details.isolation is KnowledgeIsolation.DEDICATED_INSTANCE
            or peer.isolation is KnowledgeIsolation.DEDICATED_INSTANCE
        ):
            raise BindingIsolationError("dedicated knowledge connection is shared")
        same_location = (details.table, details.namespace) == (peer.table, peer.namespace)
        if same_location and not (
            details.isolation is KnowledgeIsolation.FILTERED_SHARED_LOCATION
            and peer.isolation is KnowledgeIsolation.FILTERED_SHARED_LOCATION
        ):
            raise BindingIsolationError("shared knowledge location lacks filtering")


def _validate_artifact(binding: ResourceBinding, others: tuple[ResourceBinding, ...]) -> None:
    details = binding.details
    if not isinstance(details, ArtifactStoreBinding) or binding.status is BindingStatus.DISABLED:
        return
    for other in others:
        peer = other.details
        if (
            other.status is BindingStatus.DISABLED
            or other.key.environment != binding.key.environment
            or other.key.initiative_id == binding.key.initiative_id
            or not isinstance(peer, ArtifactStoreBinding)
            or peer.bucket_alias != details.bucket_alias
        ):
            continue
        first, second = details.prefix.rstrip("/"), peer.prefix.rstrip("/")
        if first == second or first.startswith(second + "/") or second.startswith(first + "/"):
            raise BindingIsolationError("artifact prefixes overlap across initiatives")


class InMemoryResourceBindingRepository:
    def __init__(self) -> None:
        self._records: dict[ResourceBindingKey, ResourceBinding] = {}
        self._lock = Lock()

    def insert(self, binding: ResourceBinding) -> None:
        with self._lock:
            if binding.key in self._records:
                raise BindingExistsError("resource binding already exists")
            self._validate(binding)
            self._records[binding.key] = binding

    def find(self, key: ResourceBindingKey) -> ResourceBinding | None:
        with self._lock:
            return self._records.get(key)

    def list_all(self) -> tuple[ResourceBinding, ...]:
        with self._lock:
            return tuple(self._records.values())

    def replace(self, binding: ResourceBinding, *, expected_revision: int) -> None:
        with self._lock:
            current = self._records.get(binding.key)
            if current is None:
                raise BindingNotFoundError("resource binding not found")
            if (
                type(expected_revision) is not int
                or current.revision != expected_revision
                or binding.revision != current.revision + 1
                or binding.created_at != current.created_at
            ):
                raise BindingConflictError("resource binding changed; retry")
            self._validate(binding)
            self._records[binding.key] = binding

    def _validate(self, binding: ResourceBinding) -> None:
        others = tuple(item for key, item in self._records.items() if key != binding.key)
        _validate_knowledge(binding, others)
        _validate_artifact(binding, others)


class InMemoryBindingEventSink:
    def __init__(self) -> None:
        self._events: list[BindingMutationEvent] = []
        self._lock = Lock()

    def publish(self, event: BindingMutationEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[BindingMutationEvent, ...]:
        with self._lock:
            return tuple(self._events)
