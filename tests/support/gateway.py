"""A DynamoDB client fake with atomic delete-return semantics, without AWS."""

from copy import deepcopy
from threading import Lock


class FakeDynamoDbClient:
    def __init__(self):
        self.items = {}
        self.puts = []
        self.deletes = []
        self._lock = Lock()

    def put_item(self, **kwargs):
        with self._lock:
            self.puts.append(deepcopy(kwargs))
            key = kwargs["Item"]["reference_hash"]["S"]
            assert kwargs["ConditionExpression"] == "attribute_not_exists(reference_hash)"
            if key in self.items:
                raise ValueError("conditional write rejected")
            self.items[key] = deepcopy(kwargs["Item"])

    def delete_item(self, **kwargs):
        with self._lock:
            self.deletes.append(deepcopy(kwargs))
            assert kwargs["ReturnValues"] == "ALL_OLD"
            item = self.items.pop(kwargs["Key"]["reference_hash"]["S"], None)
            return {"Attributes": item} if item else {}
