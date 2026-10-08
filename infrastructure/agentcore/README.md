# AgentCore Gateway stack

`gateway.json` is generated from the Python handler catalogs by `generate.py`. Deploy it with CloudFormation after supplying the existing runtime role name and three governed target Lambda ARNs. The target deployments and authenticated runtime session lookup are prerequisites; see [architecture](../../docs/architecture/agentcore-gateway.md).

Validate drift locally with `PYTHONPATH=src .venv/bin/python infrastructure/agentcore/generate.py` followed by `git diff --exit-code -- infrastructure/agentcore/gateway.json` (or run `pytest -q tests/test_gateway.py`). The stack requires `CAPABILITY_IAM` at deployment. A live `aws cloudformation validate-template` needs AWS CLI/network access and a supported AgentCore region.
