"""Bounded argv-only local subprocess runner for trusted workspace adapters."""

import os
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from ai_dlc.application.local_workspace.ports import WorkspaceFailure


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int


class SafeProcessRunner:
    """Finite callers choose argv; executable names resolve from trusted allowlist."""

    def __init__(self, executables: Mapping[str, Path]) -> None:
        if not executables:
            raise ValueError("approved executables required")
        self._executables = {}
        for name, path in executables.items():
            if not name or "/" in name or not isinstance(path, Path) or not path.is_file():
                raise ValueError("invalid approved executable")
            self._executables[name] = path.resolve()

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        home: Path,
        timeout_seconds: float,
        stdin: bytes | None = None,
        output_limit: int = 8192,
    ) -> ProcessOutput:
        if not argv or argv[0] not in self._executables or not 0 < timeout_seconds <= 60:
            raise WorkspaceFailure("RUNTIME_CONFIGURATION")
        if not cwd.is_dir() or not home.is_dir():
            raise WorkspaceFailure("RUNTIME_CONFIGURATION")
        executable = self._executables[argv[0]]
        env = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "PATH": os.pathsep.join(
                sorted({str(path.parent) for path in self._executables.values()})
            ),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "1",
        }
        started = perf_counter()
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                process = subprocess.Popen(
                    [str(executable), *argv[1:]],
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    shell=False,
                    start_new_session=True,
                )
                try:
                    process.communicate(input=stdin, timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    raise WorkspaceFailure("EXECUTION_TIMEOUT") from None
            except WorkspaceFailure:
                raise
            except OSError:
                raise WorkspaceFailure("RUNTIME_CONFIGURATION") from None
            out.seek(0, os.SEEK_END)
            out_size = out.tell()
            err.seek(0, os.SEEK_END)
            err_size = err.tell()
            out.seek(0)
            err.seek(0)
            return ProcessOutput(
                exit_code=process.returncode,
                stdout=out.read(output_limit).decode("utf-8", errors="replace"),
                stderr=err.read(output_limit).decode("utf-8", errors="replace"),
                stdout_truncated=out_size > output_limit,
                stderr_truncated=err_size > output_limit,
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
            )
