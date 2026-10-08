"""Local test/development workspace adapters."""

from .executor import SubprocessWorkspaceExecutor, contained_path, patch_paths
from .in_memory import InMemoryCommitHandoffStore, InMemoryWorkspaceAuditSink
from .manager import LocalMirrorMaterializer, TemporaryWorkspaceManager
from .process import SafeProcessRunner

__all__ = [
    "InMemoryCommitHandoffStore",
    "InMemoryWorkspaceAuditSink",
    "LocalMirrorMaterializer",
    "SafeProcessRunner",
    "SubprocessWorkspaceExecutor",
    "TemporaryWorkspaceManager",
    "contained_path",
    "patch_paths",
]
