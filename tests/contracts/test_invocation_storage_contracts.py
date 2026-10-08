"""Production store adapter tested against a deterministic atomic fake client."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from support.enterprise import BUSINESS_CANARY, LABELS, READ_TOOLS, read_arguments
from support.gateway import FakeDynamoDbClient

from ai_dlc.adapters.gateway import DynamoDbInvocationRecordStore

pytestmark = pytest.mark.contract


def test_issue_hashes_reference_and_stores_no_business_body_or_credentials(world):
    client = FakeDynamoDbClient()
    store = DynamoDbInvocationRecordStore(client, "contract-invocations")
    registry = world.invocation_registry(store=store)
    issued = registry.issue(
        "jira_add_comment",
        {**read_arguments("jira"), "body": BUSINESS_CANARY},
        context=world.context(),
        message_id="message-one",
    )
    key = hashlib.sha256(issued.reference.encode()).hexdigest()
    item = client.items[key]
    assert item["reference_hash"] == {"S": key}
    assert set(item) == {"reference_hash", "record", "expires_at"}
    assert client.puts[0]["TableName"] == "contract-invocations"
    assert client.puts[0]["ConditionExpression"] == "attribute_not_exists(reference_hash)"
    for forbidden in (issued.reference, BUSINESS_CANARY, "connection_alias", "token", "endpoint"):
        assert forbidden not in json.dumps(item)
    record = store.take(key)
    assert record["principal_id"] == "contract-user"
    assert client.deletes[0]["ReturnValues"] == "ALL_OLD"
    assert client.deletes[0]["Key"] == {"reference_hash": {"S": key}}
    assert store.take(key) is None


def test_conditional_put_cannot_overwrite_an_existing_invocation(world):
    client = FakeDynamoDbClient()
    store = DynamoDbInvocationRecordStore(client, "contract-invocations")
    store.put("hash", {"principal_id": "first"}, 123)
    with pytest.raises(ValueError, match="conditional"):
        store.put("hash", {"principal_id": "second"}, 124)
    assert store.take("hash") == {"principal_id": "first"}


def test_concurrent_reference_consumption_returns_only_one_context(world, domain):
    client = FakeDynamoDbClient()
    registry = world.invocation_registry(store=DynamoDbInvocationRecordStore(client, "invocations"))
    issued = registry.issue(
        READ_TOOLS[domain], read_arguments(domain), context=world.context(), message_id="message"
    )

    def consume_correct_target():
        try:
            return registry.resolve(
                issued.reference,
                gateway_id="gateway-contract",
                target_id=f"target-{LABELS[domain]}",
                tool_name=READ_TOOLS[domain],
                message_id="message",
                arguments=read_arguments(domain),
            )
        except PermissionError:
            return None

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: consume_correct_target(), range(4)))
    assert sum(result is not None for result in results) == 1
    assert client.items == {}


def test_dynamodb_ttl_is_not_required_for_expired_reference_denial(world, domain, monkeypatch):
    import ai_dlc.application.gateway.invocation as module

    client = FakeDynamoDbClient()
    registry = world.invocation_registry(store=DynamoDbInvocationRecordStore(client, "invocations"))
    issued = registry.issue(
        READ_TOOLS[domain], read_arguments(domain), context=world.context(), message_id="message"
    )
    assert len(client.items) == 1
    monkeypatch.setattr(module, "time", lambda: issued.expires_at + 1)
    assert len(client.items) == 1, "expired items remain until asynchronous TTL deletion"
    with pytest.raises(PermissionError):
        registry.resolve(
            issued.reference,
            gateway_id="gateway-contract",
            target_id=f"target-{LABELS[domain]}",
            tool_name=READ_TOOLS[domain],
            message_id="message",
            arguments=read_arguments(domain),
        )
    assert not world.calls()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"record": {"S": "[]"}},
        {"record": {"S": "{"}},
        {"record": {"N": "1"}},
        {"record": {"S": '{"expires_at": 999999999999}'}},
    ],
)
def test_malformed_stored_payload_never_returns_trusted_context(world, payload):
    client = FakeDynamoDbClient()
    registry = world.invocation_registry(store=DynamoDbInvocationRecordStore(client, "invocations"))
    issued = registry.issue(
        "jira_get_issue", read_arguments("jira"), context=world.context(), message_id="message"
    )
    key = hashlib.sha256(issued.reference.encode()).hexdigest()
    client.items[key] = payload
    with pytest.raises((PermissionError, ValueError)):
        registry.resolve(
            issued.reference,
            gateway_id="gateway-contract",
            target_id="target-Jira",
            tool_name="jira_get_issue",
            message_id="message",
            arguments=read_arguments("jira"),
        )
    assert not world.calls()
