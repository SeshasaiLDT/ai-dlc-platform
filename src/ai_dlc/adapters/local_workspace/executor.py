"""Finite local Git/build/test operations with containment and bounded output."""

import re
from pathlib import Path

from ai_dlc.application.local_workspace.models import (
    CommitResult,
    LocalDiff,
    LocalStatus,
    PatchResult,
    ProcessResult,
    WorkspaceRecord,
    WorkspaceState,
    valid_relative_path,
)
from ai_dlc.application.local_workspace.ports import WorkspaceFailure

from .process import ProcessOutput, SafeProcessRunner

_MAX_PATCH_FILES = 20
_MAX_DIFF_FILES = 100
_MAX_PATCH_BYTES = 65536
_MAX_DIFF_PATCH_BYTES = 32768
_MAX_FILE_PATCH_BYTES = 4096


def contained_path(record: WorkspaceRecord, relative: str, *, allow_missing: bool = False) -> Path:
    """Reject lexical traversal and every symlink component before touching a path."""
    if not valid_relative_path(relative):
        raise WorkspaceFailure("INVALID_ARGUMENT")
    root = record.root.resolve(strict=True)
    if record.root.is_symlink() or not (root / ".git").exists():
        raise WorkspaceFailure("WORKSPACE_NOT_ACTIVE")
    candidate = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise WorkspaceFailure("INVALID_ARGUMENT")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root) or (not allow_missing and not candidate.exists()):
        raise WorkspaceFailure("INVALID_ARGUMENT")
    return candidate


def patch_paths(patch: str) -> tuple[str, ...]:
    if len(patch.encode("utf-8")) > _MAX_PATCH_BYTES:
        raise WorkspaceFailure("PATCH_REJECTED")
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith(("GIT binary patch", "Binary files ", "rename ", "copy ")) or (
            line.startswith(("new file mode ", "old mode ", "deleted file mode "))
            and ("120000" in line or "160000" in line)
        ):
            raise WorkspaceFailure("PATCH_REJECTED")
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/([A-Za-z0-9._/-]+) b/([A-Za-z0-9._/-]+)", line)
            if match is None or match.group(1) != match.group(2):
                raise WorkspaceFailure("PATCH_REJECTED")
            path = match.group(1)
            if not valid_relative_path(path) or path in paths:
                raise WorkspaceFailure("PATCH_REJECTED")
            paths.append(path)
        elif line.startswith(("--- ", "+++ ")):
            name = line[4:]
            if name != "/dev/null":
                if not re.fullmatch(r"[ab]/[A-Za-z0-9._/-]+", name):
                    raise WorkspaceFailure("PATCH_REJECTED")
                if not paths or name[2:] != paths[-1]:
                    raise WorkspaceFailure("PATCH_REJECTED")
    if not paths or len(paths) > _MAX_PATCH_FILES:
        raise WorkspaceFailure("PATCH_REJECTED")
    return tuple(paths)


class SubprocessWorkspaceExecutor:
    def __init__(self, runner: SafeProcessRunner) -> None:
        self._runner = runner

    @staticmethod
    def _active(record: WorkspaceRecord) -> None:
        if (
            record.state is not WorkspaceState.ACTIVE
            or record.root.is_symlink()
            or not record.root.is_dir()
        ):
            raise WorkspaceFailure("WORKSPACE_NOT_ACTIVE")
        if not (record.root / ".git").exists():
            raise WorkspaceFailure("WORKSPACE_NOT_ACTIVE")

    def _git(
        self,
        record: WorkspaceRecord,
        args: tuple[str, ...],
        timeout_seconds: float,
        *,
        stdin: bytes | None = None,
        limit: int = 8192,
    ) -> ProcessOutput:
        self._active(record)
        return self._runner.run(
            ("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args),
            cwd=record.root,
            home=record.home,
            timeout_seconds=timeout_seconds,
            stdin=stdin,
            output_limit=limit,
        )

    @staticmethod
    def _ok(result: ProcessOutput, code: str = "EXECUTION_FAILED") -> None:
        if result.exit_code != 0:
            raise WorkspaceFailure(code)

    def checkout(self, record: WorkspaceRecord, branch: str, timeout_seconds: float) -> None:
        args = (
            ("switch", "--detach", "--", branch)
            if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", branch)
            else ("switch", "--", branch)
        )
        self._ok(self._git(record, args, timeout_seconds))

    def status(self, record: WorkspaceRecord, timeout_seconds: float) -> LocalStatus:
        result = self._git(
            record,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            timeout_seconds,
            limit=1048576,
        )
        self._ok(result)
        if result.stdout_truncated:
            raise WorkspaceFailure("EXECUTION_FAILED")
        staged: list[str] = []
        modified: list[str] = []
        untracked: list[str] = []
        tokens = result.stdout.rstrip("\0").split("\0") if result.stdout else []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if len(token) < 4 or token[2] != " ":
                raise WorkspaceFailure("EXECUTION_FAILED")
            code, path = token[:2], token[3:]
            if not valid_relative_path(path):
                raise WorkspaceFailure("EXECUTION_FAILED")
            if code == "??":
                untracked.append(path)
            else:
                if code[0] != " ":
                    staged.append(path)
                if code[1] != " ":
                    modified.append(path)
                if "R" in code or "C" in code:
                    index += 1  # porcelain -z includes the original path after a rename/copy
            index += 1
        total = len(staged) + len(modified) + len(untracked)
        omitted = sum(max(0, len(items) - 100) for items in (staged, modified, untracked))
        head = self._git(record, ("rev-parse", "HEAD"), timeout_seconds)
        branch = self._git(record, ("rev-parse", "--abbrev-ref", "HEAD"), timeout_seconds)
        self._ok(head)
        self._ok(branch)
        return LocalStatus(
            repository_id=record.key.repository_id,
            branch=branch.stdout.strip(),
            head_sha=head.stdout.strip(),
            staged_files=tuple(staged[:100]),
            modified_files=tuple(modified[:100]),
            untracked_files=tuple(untracked[:100]),
            omitted_file_count=omitted,
            truncated=omitted > 0,
            clean=total == 0,
        )

    def diff(self, record: WorkspaceRecord, timeout_seconds: float) -> LocalDiff:
        names = self._git(
            record,
            ("diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", "HEAD", "--"),
            timeout_seconds,
            limit=1048576,
        )
        self._ok(names)
        if names.stdout_truncated:
            raise WorkspaceFailure("EXECUTION_FAILED")
        paths = names.stdout.rstrip("\0").split("\0") if names.stdout else []
        if any(not valid_relative_path(path) for path in paths):
            raise WorkspaceFailure("EXECUTION_FAILED")
        selected = paths[:_MAX_DIFF_FILES]
        omitted = len(paths) - len(selected)
        budget = _MAX_DIFF_PATCH_BYTES
        patches: list[str] = []
        truncated = omitted > 0
        for path in selected:
            result = self._git(
                record,
                ("diff", "--no-ext-diff", "--no-textconv", "HEAD", "--", path),
                timeout_seconds,
                limit=_MAX_FILE_PATCH_BYTES + 1,
            )
            self._ok(result)
            data = result.stdout.encode("utf-8")
            take = min(len(data), _MAX_FILE_PATCH_BYTES, budget)
            patches.append(data[:take].decode("utf-8", errors="ignore"))
            if result.stdout_truncated or len(data) > take:
                truncated = True
            budget -= take
        return LocalDiff(
            repository_id=record.key.repository_id,
            patch="".join(patches),
            changed_files=tuple(selected),
            omitted_file_count=omitted,
            truncated=truncated,
        )

    def apply_patch(
        self, record: WorkspaceRecord, patch: str, timeout_seconds: float
    ) -> PatchResult:
        paths = patch_paths(patch)
        try:
            for path in paths:
                contained_path(record, path, allow_missing=True)
        except WorkspaceFailure:
            raise WorkspaceFailure("PATCH_REJECTED") from None
        payload = patch.encode("utf-8")
        self._ok(
            self._git(record, ("apply", "--check", "--"), timeout_seconds, stdin=payload),
            "PATCH_REJECTED",
        )
        self._ok(
            self._git(record, ("apply", "--"), timeout_seconds, stdin=payload),
            "PATCH_REJECTED",
        )
        return PatchResult(repository_id=record.key.repository_id, changed_files=paths)

    def run_configured(
        self,
        record: WorkspaceRecord,
        operation: str,
        argv: tuple[str, ...],
        working_directory: str,
        timeout_seconds: float,
    ) -> ProcessResult:
        self._active(record)
        cwd = record.root if working_directory == "." else contained_path(record, working_directory)
        if not cwd.is_dir():
            raise WorkspaceFailure("INVALID_ARGUMENT")
        output = self._runner.run(
            argv,
            cwd=cwd,
            home=record.home,
            timeout_seconds=timeout_seconds,
            output_limit=8192,
        )
        return ProcessResult(
            repository_id=record.key.repository_id,
            operation=operation,
            exit_code=output.exit_code,
            success=output.exit_code == 0,
            duration_ms=output.duration_ms,
            stdout=output.stdout,
            stderr=output.stderr,
            stdout_truncated=output.stdout_truncated,
            stderr_truncated=output.stderr_truncated,
        )

    def commit(
        self,
        record: WorkspaceRecord,
        message: str,
        paths: tuple[str, ...],
        timeout_seconds: float,
    ) -> CommitResult:
        for path in paths:
            contained_path(record, path, allow_missing=True)
        self._ok(self._git(record, ("reset", "--mixed", "HEAD"), timeout_seconds))
        self._ok(self._git(record, ("add", "--all", "--", *paths), timeout_seconds))
        diff = self._git(record, ("diff", "--cached", "--quiet", "--"), timeout_seconds)
        if diff.exit_code == 0:
            raise WorkspaceFailure("EXECUTION_FAILED")
        if diff.exit_code != 1:
            raise WorkspaceFailure("EXECUTION_FAILED")
        self._ok(
            self._git(
                record,
                (
                    "-c",
                    "user.name=AI-DLC",
                    "-c",
                    "user.email=ai-dlc@invalid.example",
                    "commit",
                    "--no-verify",
                    "-m",
                    message,
                ),
                timeout_seconds,
            )
        )
        sha = self._git(record, ("rev-parse", "HEAD"), timeout_seconds)
        self._ok(sha)
        return CommitResult(repository_id=record.key.repository_id, commit_sha=sha.stdout.strip())
