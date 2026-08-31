"""DynamoDB access: orders and feature flags.

Single-table design. pk carries the entity type:
    ORDER#<id>   an order record
    FLAG#<name>  a feature flag / armed chaos mode
"""

import os
import time
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config

TABLE_NAME = os.environ["TABLE_NAME"]

_ddb = boto3.resource(
    "dynamodb",
    config=Config(retries={"max_attempts": 3, "mode": "standard"}),
)
_table = _ddb.Table(TABLE_NAME)


def _clean(obj: Any) -> Any:
    """DynamoDB returns Decimal; make it JSON-serialisable."""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


# --- orders ------------------------------------------------------------------

def put_order(order_id: str, payload: dict) -> None:
    _table.put_item(
        Item={
            "pk": f"ORDER#{order_id}",
            "order_id": order_id,
            "status": "PENDING",
            "created_at": int(time.time()),
            **payload,
        }
    )


def get_order(order_id: str) -> dict | None:
    resp = _table.get_item(Key={"pk": f"ORDER#{order_id}"})
    item = resp.get("Item")
    return _clean(item) if item else None


def update_order_status(order_id: str, status: str, note: str | None = None) -> None:
    expr = "SET #s = :s, updated_at = :t"
    values: dict[str, Any] = {":s": status, ":t": int(time.time())}
    if note:
        expr += ", note = :n"
        values[":n"] = note
    _table.update_item(
        Key={"pk": f"ORDER#{order_id}"},
        UpdateExpression=expr,
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=values,
    )


# --- flags and armed chaos ---------------------------------------------------

def get_flag(name: str) -> dict | None:
    """Returns the flag record, or None if unset/expired/exhausted."""
    resp = _table.get_item(Key={"pk": f"FLAG#{name}"})
    item = resp.get("Item")
    if not item:
        return None

    # self-disarm so a chaos mode can't be left on by accident
    expires = item.get("expires_at")
    if expires and int(expires) < int(time.time()):
        return None
    remaining = item.get("remaining")
    if remaining is not None and int(remaining) <= 0:
        return None

    return _clean(item)


def set_flag(name: str, enabled: bool, ttl_seconds: int = 900,
             remaining: int | None = None, **extra: Any) -> dict:
    item: dict[str, Any] = {
        "pk": f"FLAG#{name}",
        "name": name,
        "enabled": enabled,
        "set_at": int(time.time()),
        "expires_at": int(time.time()) + ttl_seconds,
        **extra,
    }
    if remaining is not None:
        item["remaining"] = remaining
    _table.put_item(Item=item)
    return _clean(item)


def consume_flag(name: str) -> None:
    """Decrement a flag's remaining-uses counter. No-op if it has none."""
    try:
        _table.update_item(
            Key={"pk": f"FLAG#{name}"},
            UpdateExpression="SET remaining = remaining - :one",
            ConditionExpression="attribute_exists(remaining) AND remaining > :zero",
            ExpressionAttributeValues={":one": 1, ":zero": 0},
        )
    except _ddb.meta.client.exceptions.ConditionalCheckFailedException:
        pass


def clear_flag(name: str) -> None:
    _table.delete_item(Key={"pk": f"FLAG#{name}"})


def list_flags() -> list[dict]:
    resp = _table.scan(
        FilterExpression="begins_with(pk, :p)",
        ExpressionAttributeValues={":p": "FLAG#"},
    )
    return [_clean(i) for i in resp.get("Items", [])]
