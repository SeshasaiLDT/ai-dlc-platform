"""Atomic one-use invocation stores for tests and shared AWS deployments."""

import json
from threading import Lock
from typing import Protocol


class InMemoryInvocationRecordStore:
    def __init__(self) -> None:
        self._records: dict[str, dict[str, object]] = {}
        self._lock = Lock()

    def put(self, key: str, record: dict[str, object], expires_at: int) -> None:
        with self._lock:
            if key in self._records:
                raise ValueError("duplicate invocation reference")
            self._records[key] = dict(record)

    def take(self, key: str) -> dict[str, object] | None:
        with self._lock:
            return self._records.pop(key, None)


class DynamoDbClient(Protocol):
    def put_item(self, **kwargs: object) -> object: ...

    def delete_item(self, **kwargs: object) -> dict[str, object]: ...


class DynamoDbInvocationRecordStore:
    """Use conditional writes and atomic delete-return for cross-Lambda replay protection.

    The injected client is a boto3 DynamoDB client; this module has no SDK dependency.
    """

    def __init__(self, client: DynamoDbClient, table_name: str) -> None:
        if not table_name:
            raise ValueError("invocation table required")
        self._client = client
        self._table_name = table_name

    def put(self, key: str, record: dict[str, object], expires_at: int) -> None:
        self._client.put_item(
            TableName=self._table_name,
            Item={
                "reference_hash": {"S": key},
                "record": {"S": json.dumps(record, sort_keys=True, separators=(",", ":"))},
                "expires_at": {"N": str(expires_at)},
            },
            ConditionExpression="attribute_not_exists(reference_hash)",
        )

    def take(self, key: str) -> dict[str, object] | None:
        response = self._client.delete_item(
            TableName=self._table_name,
            Key={"reference_hash": {"S": key}},
            ReturnValues="ALL_OLD",
        )
        attributes = response.get("Attributes")
        if not isinstance(attributes, dict):
            return None
        value = attributes.get("record")
        if not isinstance(value, dict) or not isinstance(value.get("S"), str):
            raise PermissionError("invalid stored invocation")
        decoded = json.loads(value["S"])
        if not isinstance(decoded, dict):
            raise PermissionError("invalid stored invocation")
        return decoded
