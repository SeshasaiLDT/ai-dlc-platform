"""One-use runtime-to-target correlation, independent of the shared IAM role."""

import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from time import time
from typing import Protocol

from ai_dlc.application.authorization import ScopeRestriction
from ai_dlc.domain.identity import Principal, ToolPermission

from .context import AuthenticatedRuntimeSelection, GatewayContextResolver, TrustedGatewayContext

INVOCATION_REF_FIELD = "aidlcInvocationRef"


def _digest(arguments: Mapping[str, object]) -> str:
    try:
        data = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("tool arguments must be JSON serializable") from exc
    return hashlib.sha256(data.encode()).hexdigest()


class InvocationRecordStore(Protocol):
    def put(self, key: str, record: dict[str, object], expires_at: int) -> None: ...

    def take(self, key: str) -> dict[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class IssuedInvocation:
    reference: str
    expires_at: int


class TrustedInvocationRegistry:
    """Issue and atomically consume opaque references through a shared store."""

    def __init__(
        self,
        store: InvocationRecordStore,
        resolver: GatewayContextResolver,
        *,
        gateway_id: str,
        target_ids: Mapping[str, str],
        ttl_seconds: int = 60,
    ) -> None:
        if not gateway_id or not target_ids or not 1 <= ttl_seconds <= 300:
            raise ValueError("invalid invocation registry configuration")
        self._store = store
        self._resolver = resolver
        self._gateway_id = gateway_id
        self._target_ids = dict(target_ids)
        self._ttl = ttl_seconds

    def issue(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        context: TrustedGatewayContext,
        message_id: str,
    ) -> IssuedInvocation:
        if not isinstance(context, TrustedGatewayContext):
            raise TypeError("trusted gateway context required")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("runtime-generated MCP message ID required")
        target_name = self._target_for(tool_name)
        auth = context.resolution.authorization
        scopes = auth.allowed_scopes
        expires_at = int(time()) + self._ttl
        reference = secrets.token_urlsafe(32)
        record: dict[str, object] = {
            "gateway_id": self._gateway_id,
            "target_id": self._target_ids[target_name],
            "tool_name": tool_name,
            "message_id": message_id,
            "arguments_digest": _digest(arguments),
            "expires_at": expires_at,
            "principal_id": auth.principal.subject_id,
            "principal_provider": auth.principal.provider,
            "initiative_id": auth.initiative_id,
            "environment": context.resolution.environment,
            "correlation_id": context.resolution.correlation_id,
            "initiative_revision": context.initiative_revision,
            "workspace_id": context.workspace_id,
            "task_id": context.task_id,
            "approval_id": context.approval_id,
            "approved_commits": sorted([list(item) for item in context.approved_commits]),
            "timeout_seconds": context.timeout_seconds,
            "cancelled": context.cancelled,
            "permissions": sorted(item.value for item in auth.tool_permissions),
            "scopes": {
                name: sorted(getattr(scopes, name))
                for name in (
                    "jira_projects",
                    "repository_ids",
                    "knowledge_source_ids",
                    "servicenow_scopes",
                    "artifact_store_ids",
                )
            },
        }
        self._store.put(hashlib.sha256(reference.encode()).hexdigest(), record, expires_at)
        return IssuedInvocation(reference, expires_at)

    def resolve(
        self,
        reference: str,
        *,
        gateway_id: str,
        target_id: str,
        tool_name: str,
        message_id: str,
        arguments: Mapping[str, object],
    ) -> TrustedGatewayContext:
        if not isinstance(reference, str) or len(reference) < 40:
            raise PermissionError("trusted invocation reference required")
        record = self._store.take(hashlib.sha256(reference.encode()).hexdigest())
        if record is None:
            raise PermissionError("trusted invocation unavailable")
        try:
            if (
                int(record["expires_at"]) <= time()
                or record["gateway_id"] != gateway_id
                or record["target_id"] != target_id
                or record["tool_name"] != tool_name
                or record["message_id"] != message_id
                or record["arguments_digest"] != _digest(arguments)
            ):
                raise PermissionError("trusted invocation mismatch")
            selection = AuthenticatedRuntimeSelection(
                Principal(str(record["principal_id"]), str(record["principal_provider"])),
                str(record["initiative_id"]),
                str(record["environment"]),
                str(record["correlation_id"]),
                workspace_id=record["workspace_id"],
                task_id=record["task_id"],
                approval_id=record["approval_id"],
                scope_restriction=ScopeRestriction(
                    **{key: frozenset(value) for key, value in record["scopes"].items()}
                ),
                approved_commits=frozenset(tuple(item) for item in record["approved_commits"]),
                timeout_seconds=float(record["timeout_seconds"]),
                cancelled=bool(record["cancelled"]),
            )
            context = self._resolver.resolve(selection)
            auth = context.resolution.authorization
            issued_permissions = frozenset(ToolPermission(value) for value in record["permissions"])
            if (
                context.initiative_revision != record["initiative_revision"]
                or auth.principal.subject_id != record["principal_id"]
                or auth.principal.provider != record["principal_provider"]
                or auth.initiative_id != record["initiative_id"]
                or not auth.tool_permissions <= issued_permissions
            ):
                raise PermissionError("trusted invocation authorization changed")
            return context
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError("invalid trusted invocation state") from exc

    def _target_for(self, tool_name: str) -> str:
        domain = tool_name.split("_", 1)[0]
        name = {"jira": "Jira", "servicenow": "ServiceNow", "git": "RemoteGit"}.get(domain)
        if name is None or name not in self._target_ids:
            raise ValueError("unconfigured enterprise tool target")
        return name
