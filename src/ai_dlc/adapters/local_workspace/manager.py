"""Task-scoped temporary workspaces sourced from trusted local mirrors."""

import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from ai_dlc.application.local_workspace.models import (
    WorkspaceKey,
    WorkspaceRecord,
    WorkspaceState,
    valid_branch,
)
from ai_dlc.application.local_workspace.ports import WorkspaceFailure
from ai_dlc.application.resource_bindings import GitRepositoryBinding
from ai_dlc.domain.initiative.enums import GitProvider

from .process import SafeProcessRunner


class LocalMirrorMaterializer:
    """Test/local mirror lookup keyed by trusted binding coordinates, never URL input."""

    def __init__(
        self,
        mirrors_root: Path,
        sources: Mapping[tuple[GitProvider, str, str], Path],
        runner: SafeProcessRunner,
    ) -> None:
        self._root = mirrors_root.resolve(strict=True)
        self._sources = {}
        self._runner = runner
        for key, path in sources.items():
            source = path.resolve(strict=True)
            if not source.is_relative_to(self._root) or not source.is_dir():
                raise ValueError("mirror source must be inside trusted mirrors root")
            self._sources[key] = source

    def materialize(
        self,
        binding: GitRepositoryBinding,
        destination: Path,
        home: Path,
        default_branch: str,
        timeout_seconds: float,
    ) -> None:
        source = self._sources.get((binding.provider, binding.owner, binding.repository))
        if source is None or not valid_branch(default_branch):
            raise WorkspaceFailure("RESOURCE_NOT_FOUND")
        result = self._runner.run(
            (
                "git",
                "clone",
                "--no-hardlinks",
                "--branch",
                default_branch,
                "--",
                str(source),
                str(destination),
            ),
            cwd=destination.parent,
            home=home,
            timeout_seconds=timeout_seconds,
        )
        if result.exit_code != 0:
            raise WorkspaceFailure("EXECUTION_FAILED")


class TemporaryWorkspaceManager:
    def __init__(self, base_root: Path, materializer: LocalMirrorMaterializer) -> None:
        self._base = base_root.resolve(strict=True)
        self._materializer = materializer
        self._records: dict[WorkspaceKey, WorkspaceRecord] = {}
        self._lock = RLock()

    def prepare(
        self,
        key: WorkspaceKey,
        owner_id: str,
        binding: GitRepositoryBinding,
        default_branch: str,
        timeout_seconds: float,
    ) -> WorkspaceRecord:
        with self._lock:
            current = self._records.get(key)
            if current is not None:
                if current.owner_principal_id != owner_id:
                    raise WorkspaceFailure("PERMISSION_DENIED")
                if current.state is not WorkspaceState.ACTIVE:
                    raise WorkspaceFailure("WORKSPACE_NOT_ACTIVE")
                self._check_active_root(current)
                return current
            container = Path(tempfile.mkdtemp(prefix="aidlc-task-", dir=self._base))
            home = container / "home"
            home.mkdir(mode=0o700)
            root = container / "repo"
            try:
                self._materializer.materialize(binding, root, home, default_branch, timeout_seconds)
                record = WorkspaceRecord(
                    key,
                    owner_id,
                    root,
                    home,
                    WorkspaceState.ACTIVE,
                    default_branch,
                    datetime.now(UTC),
                )
                self._records[key] = record
                return record
            except Exception:
                try:
                    shutil.rmtree(container)
                except OSError:
                    self._records[key] = WorkspaceRecord(
                        key,
                        owner_id,
                        root,
                        home,
                        WorkspaceState.FAILED,
                        default_branch,
                        datetime.now(UTC),
                    )
                    raise WorkspaceFailure("EXECUTION_FAILED") from None
                raise

    def get(self, key: WorkspaceKey, owner_id: str) -> WorkspaceRecord | None:
        with self._lock:
            record = self._records.get(key)
            if record is not None and record.owner_principal_id != owner_id:
                raise WorkspaceFailure("PERMISSION_DENIED")
            if record is not None and record.state is WorkspaceState.ACTIVE:
                self._check_active_root(record)
            return record

    def _check_active_root(self, record: WorkspaceRecord) -> None:
        container = record.root.parent
        if (
            container.is_symlink()
            or record.root.is_symlink()
            or (record.root / ".git").is_symlink()
            or not container.resolve().is_relative_to(self._base)
            or not record.root.is_dir()
            or not (record.root / ".git").is_dir()
        ):
            self._records[record.key] = replace(record, state=WorkspaceState.FAILED)
            raise WorkspaceFailure("WORKSPACE_NOT_ACTIVE")

    def set_branch(self, key: WorkspaceKey, owner_id: str, branch: str) -> WorkspaceRecord:
        with self._lock:
            record = self.get(key, owner_id)
            if record is None:
                raise WorkspaceFailure("WORKSPACE_NOT_FOUND")
            if record.state is not WorkspaceState.ACTIVE:
                raise WorkspaceFailure("WORKSPACE_NOT_ACTIVE")
            updated = replace(record, branch=branch)
            self._records[key] = updated
            return updated

    def cleanup(self, key: WorkspaceKey, owner_id: str) -> WorkspaceRecord:
        with self._lock:
            record = self.get(key, owner_id)
            if record is None:
                raise WorkspaceFailure("WORKSPACE_NOT_FOUND")
            if record.state is WorkspaceState.CLEANED:
                return record
            container = record.root.parent
            if not container.resolve().is_relative_to(self._base) or container == self._base:
                raise WorkspaceFailure("RUNTIME_CONFIGURATION")
            self._records[key] = replace(record, state=WorkspaceState.CLEANING)
            try:
                shutil.rmtree(container)
            except OSError:
                failed = replace(record, state=WorkspaceState.FAILED)
                self._records[key] = failed
                raise WorkspaceFailure("EXECUTION_FAILED") from None
            cleaned = replace(record, state=WorkspaceState.CLEANED)
            self._records[key] = cleaned
            return cleaned
