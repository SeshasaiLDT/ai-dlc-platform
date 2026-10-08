"""Safe structured Gateway telemetry with no business arguments or target details."""

import json
import logging

from ai_dlc.application.gateway.router import GatewayTelemetryEvent


class JsonGatewayTelemetrySink:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def record(self, event: GatewayTelemetryEvent) -> None:
        if not isinstance(event, GatewayTelemetryEvent):
            raise TypeError("gateway telemetry event required")
        self._logger.info(
            json.dumps(
                {
                    "correlation_id": event.correlation_id,
                    "principal_id": event.principal_id,
                    "initiative_id": event.initiative_id,
                    "action": event.action,
                    "tool_name": event.tool_name,
                    "outcome": event.outcome,
                    "latency_ms": event.latency_ms,
                },
                sort_keys=True,
            )
        )
