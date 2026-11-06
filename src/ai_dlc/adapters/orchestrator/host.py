"""AgentCore HTTP contract host with an explicitly composed application service."""

import importlib
import json
import os
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from ai_dlc.application.agent_harness.models import ContractModel
from ai_dlc.application.authentication import AuthenticationService, BearerCredential
from ai_dlc.application.orchestrator.service import Orchestrator, SingleAgentRequest
from ai_dlc.application.planning import PlanError, PlanningRequest, PlanningService


class PlanInvocation(ContractModel):
    operation: Literal["plan"]
    request: PlanningRequest


@dataclass(frozen=True)
class Dependencies:
    authentication: AuthenticationService
    orchestrator: Orchestrator
    planning: PlanningService | None = None


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
                    payload = json.loads(bytes(data))
                    if isinstance(payload, dict) and payload.get("operation") == "plan":
                        request = PlanInvocation.model_validate(payload)
                    else:
                        request = SingleAgentRequest.model_validate(payload)
                except (ValidationError, ValueError):
                    status, body = 400, {"error": "invalid_request"}
                else:
                    if isinstance(request, PlanInvocation):
                        if self.dependencies.planning is None:
                            status, body = 503, {"error": "planning_not_configured"}
                        else:
                            try:
                                result = self.dependencies.planning.accept(
                                    request.request, principal
                                )
                                status, body = 200, result.model_dump(mode="json")
                            except PlanError as error:
                                status = 403 if error.code == "authorization_failed" else 409
                                if error.code == "plan_persistence_failed":
                                    status = 503
                                body = {"error": error.code}
                            except ValueError:
                                status, body = 400, {"error": "invalid_plan"}
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
