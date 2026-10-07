"""Swappable storage and audit ports for trusted binding management."""

from typing import Protocol

from .models import BindingMutationEvent, ResourceBinding, ResourceBindingKey


class ResourceBindingRepository(Protocol):
    def insert(self, binding: ResourceBinding) -> None: ...

    def find(self, key: ResourceBindingKey) -> ResourceBinding | None: ...

    def list_all(self) -> tuple[ResourceBinding, ...]: ...

    def replace(self, binding: ResourceBinding, *, expected_revision: int) -> None: ...


class BindingEventSink(Protocol):
    def publish(self, event: BindingMutationEvent) -> None: ...
