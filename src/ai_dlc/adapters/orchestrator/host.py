"""AgentCore HTTP contract host with an explicitly composed application service."""

import importlib
import json
import os
from dataclasses import dataclass

from pydantic import ValidationError

from ai_dlc.application.authentication import AuthenticationService, BearerCredential
from ai_dlc.application.orchestrator.service import Orchestrator, SingleAgentRequest


@dataclass(frozen=True)
class Dependencies:
    authentication: AuthenticationService
    orchestrator: Orchestrator


class RuntimeApp:
    def __init__(self, dependencies: Dependencies):
        self.dependencies = dependencies

    async def __call__(self, scope, receive, send):
        status, body = 404, {"error": "not_found"}
        if scope["type"] != "http":
            return
        if scope["path"] == "/ping" and scope["method"] == "GET":
            status, body = 200, {"status": "Healthy"}
        elif scope["path"] == "/invocations" and scope["method"] == "POST":
            try:
                headers = dict(scope.get("headers", []))
                header = headers.get(b"authorization", b"").decode()
                if not header.startswith("Bearer "):
                    raise ValueError("missing credential")
                principal = self.dependencies.authentication.authenticate(
                    BearerCredential(header[7:])
                )
            except Exception:
                status, body = 401, {"error": "authentication_failed"}
            else:
                data = bytearray()
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    data.extend(event.get("body", b""))
                    if len(data) > 1_048_576:
                        break
                    if not event.get("more_body", False):
                        break
                try:
                    if len(data) > 1_048_576:
                        raise ValueError("payload too large")
                    request = SingleAgentRequest.model_validate_json(bytes(data))
                except (ValidationError, ValueError):
                    status, body = 400, {"error": "invalid_request"}
                else:
                    result = await self.dependencies.orchestrator.execute(request, principal)
                    status, body = 200, result.model_dump(mode="json")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def create_app():
    """Deployment-owned Python factory supplies repositories, policy and credential adapters."""
    module, name = os.environ["AIDLC_ORCHESTRATOR_FACTORY"].split(":", 1)
    dependencies = getattr(importlib.import_module(module), name)()
    if not isinstance(dependencies, Dependencies):
        raise TypeError("factory must return Dependencies")
    return RuntimeApp(dependencies)
