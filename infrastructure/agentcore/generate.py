"""Regenerate the checked-in CloudFormation template from handler request schemas."""

import json
from pathlib import Path

from ai_dlc.adapters.gateway import synthesize_gateway_template
from ai_dlc.application.gateway import GatewayCatalog

path = Path(__file__).with_name("gateway.json")
path.write_text(
    json.dumps(synthesize_gateway_template(GatewayCatalog.from_handlers()), indent=2) + "\n"
)
print(path)
