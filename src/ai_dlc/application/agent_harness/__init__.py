"""Public shared Agent Harness contracts and lifecycle utilities.

A2A names are loaded lazily so the core SDK imports without the optional ``a2a`` extra.
"""

from importlib import import_module
from typing import Any

from .context import create_agent_context
from .context_budget import (
    AssembledContext,
    AssembledSegment,
    BudgetFailure,
    BudgetReport,
    ContentFormat,
    ContextAssembler,
    ContextCategory,
    ContextPolicy,
    ContextSegment,
    EstimatingTokenCounter,
    ExclusionRecord,
    InstructionAuthority,
    ModelCapability,
    SourceReference,
    TokenCount,
    TokenCounter,
)
from .lifecycle import LifecycleRunner
from .mcp import McpClient, ToolDiscoveryError
from .models import (
    INTERFACE_VERSION,
    AgentContext,
    ApprovalIntent,
    ApprovalReference,
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    Invocation,
    ValidationResult,
)
from .ports import (
    AgentDelegator,
    AgentLifecycle,
    ApprovalProvider,
    ModelProvider,
    TelemetryProvider,
    ToolProvider,
    Validator,
)
from .resilience import (
    AttemptInfo,
    ErrorClass,
    ErrorClassifier,
    IdempotencyStore,
    InMemoryIdempotencyStore,
    OperationCategory,
    OperationIdentity,
    OperationRecord,
    OperationSpec,
    OperationState,
    ReconciliationOutcome,
    ReplaySafety,
    ResilientExecutor,
    ResumeAction,
    RetryPolicy,
    backoff_delay,
    derive_operation_identity,
    resume_action,
)
from .structured_output import ArtifactReference, StructuredOutputValidator

__all__ = [
    "AttemptInfo",
    "ErrorClass",
    "ErrorClassifier",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "OperationCategory",
    "OperationIdentity",
    "OperationRecord",
    "OperationSpec",
    "OperationState",
    "ReconciliationOutcome",
    "ReplaySafety",
    "ResilientExecutor",
    "ResumeAction",
    "backoff_delay",
    "derive_operation_identity",
    "resume_action",
    "AssembledContext",
    "AssembledSegment",
    "BudgetFailure",
    "BudgetReport",
    "ContentFormat",
    "ContextAssembler",
    "ContextCategory",
    "ContextPolicy",
    "ContextSegment",
    "EstimatingTokenCounter",
    "ExclusionRecord",
    "InstructionAuthority",
    "ModelCapability",
    "SourceReference",
    "TokenCount",
    "TokenCounter",
    "A2AClient",
    "A2AServerAdapter",
    "INTERFACE_VERSION",
    "AgentContext",
    "AgentDelegator",
    "AgentLifecycle",
    "ArtifactReference",
    "ConfiguredAgentDirectory",
    "ApprovalIntent",
    "ApprovalProvider",
    "ApprovalReference",
    "ExecutionError",
    "ExecutionResult",
    "ExecutionStatus",
    "Invocation",
    "LifecycleRunner",
    "McpClient",
    "ModelProvider",
    "RetryPolicy",
    "StructuredOutputValidator",
    "TelemetryProvider",
    "ToolProvider",
    "ToolDiscoveryError",
    "ValidationResult",
    "Validator",
    "build_a2a_handler",
    "create_agent_context",
]

_A2A_NAMES = frozenset(
    {"A2AClient", "A2AServerAdapter", "ConfiguredAgentDirectory", "build_a2a_handler"}
)


def __getattr__(name: str) -> Any:
    if name in _A2A_NAMES:
        try:
            module = import_module(".a2a", __name__)
        except ModuleNotFoundError as error:
            raise ImportError(
                f"{name} requires the optional A2A dependencies: "
                "pip install 'ai-dlc-agent-harness[a2a]'"
            ) from error
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
