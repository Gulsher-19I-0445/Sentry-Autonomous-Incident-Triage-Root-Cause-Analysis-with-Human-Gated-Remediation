"""Incident store — DynamoDB state machine for the whole pipeline.
 
Deduplication is done with a conditional write rather than a lock: the incident
id is *deterministic* (alarm name + time bucket), so the first writer wins and
every later attempt for the same alarm in the same window fails the condition.
No races, no coordination, works identically whether one alarm fires or four.
 
Table: sentry-capstone-incidents-gulsher
    pk = INCIDENT#<incident_id>
"""

import os
import boto3
import time
from botocore.config import Config
from enum import StrEnum
from decimal import Decimal
from botocore.exceptions import ClientError
from typing import Any


TABLE_NAME = os.environ["INCIDENTS_TABLE"]
DEDUP_WINDOW_SECONDS = int(os.environ.get("DEDUP_WINDOW_SECONDS", "300"))
TTL_DAYS = int(os.environ.get("INCIDENT_TTL_DAYS", "30"))


_ddb = boto3.resource("dynamodb", config=Config(retries={"max_attempts": 3, "mode": "standard"}))
_table = _ddb.Table(TABLE_NAME)


class Status(StrEnum):
    NEW= "NEW"
    INVESTIGATING = "INVESTIGATING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    ESCALATED = "ESCALATED"
    INFORMATIONAL = "INFORMATIONAL"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


ALLOWED_TRANSITIONS: dict[Status, set[Status]]={
    Status.NEW: {Status.INVESTIGATING, Status.FAILED},
    Status.INVESTIGATING: {Status.PENDING_APPROVAL, Status.ESCALATED, Status.INFORMATIONAL, Status.FAILED},
    Status.PENDING_APPROVAL: {Status.APPROVED, Status.REJECTED},
    Status.APPROVED: {Status.EXECUTED, Status.FAILED},
    Status.EXECUTED: {Status.CLOSED},
    Status.REJECTED: {Status.CLOSED},
    Status.ESCALATED: {Status.CLOSED},
    Status.INFORMATIONAL: {Status.CLOSED},
    Status.FAILED: {Status.CLOSED},
    Status.CLOSED: set(),
}


class IllegalTransition:
    pass

class DuplicateIncident(Exception):
    """Raised when this alarm+window already has an incident. Expected, not an error."""
 
 
def _clean(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _to_ddb(obj):
    """DynamoDB rejects floats. Convert on write; _clean converts back on read."""
    if isinstance(obj, float):
        return Decimal(str(obj))      # str() avoids float binary noise
    if isinstance(obj, dict):
        return {k: _to_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_ddb(v) for v in obj]
    return obj

 
def build_incident_id(alarm_name: str, triggered_at: int) -> str:
    """Deterministic id: same alarm in the same window -> same id -> dedup."""
    bucket = (triggered_at // DEDUP_WINDOW_SECONDS) * DEDUP_WINDOW_SECONDS
    slug = alarm_name.replace("sentry-capstone-", "").replace("-gulsher", "")
    return f"{slug}-{bucket}"


def create_incident(incident_id: str, alarm: dict):
    now = int(time.time())
    item = {
        "pk": f"INCIDENT#{incident_id}",
        "incident_id": incident_id,
        "status": Status.NEW.value,
        "alarm_name": alarm.get("AlarmName"),
        "alarm_arn": alarm.get("alarmArn"),
        "state_reason": alarm.get("NewStateReason"),
        "metric_name": (alarm.get("Trigger") or {}).get("MetricName"),
        "namespace": (alarm.get("Trigger") or {}).get("Namespace"),
        "dimensions": (alarm.get("Trigger") or {}).get("Dimensions", []),
        "triggered_at": alarm.get("_triggered_at"),
        "created_at": now,
        "updated_at": now,
        "suppressed_count": 0,
        "ttl": now + TTL_DAYS * 86400,
    }

    try:
        _table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise DuplicateIncident(incident_id) from exc
        raise
    return _clean(item)

def record_suppression(incident_id: str) -> int:
    """Count how many duplicate alarms this incident absorbed. Useful evidence:
    a high count means the alarm was flapping."""
    resp = _table.update_item(
        Key={"pk": f"INCIDENT#{incident_id}"},
        UpdateExpression="SET suppressed_count = if_not_exists(suppressed_count, :z) + :one, updated_at = :t",
        ExpressionAttributeValues={":one": 1, ":z": 0, ":t": int(time.time())},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["suppressed_count"])
 
 
def get_incident(incident_id: str) -> dict | None:
    resp = _table.get_item(Key={"pk": f"INCIDENT#{incident_id}"})
    item = resp.get("Item")
    return _clean(item) if item else None
 
 
def transition(incident_id: str, to: Status, **fields: Any) -> dict:
    """Move an incident to a new status, rejecting illegal transitions atomically."""
    legal_from = [s.value for s, targets in ALLOWED_TRANSITIONS.items() if to in targets]
    if not legal_from:
        raise IllegalTransition(f"nothing may transition to {to}")
 
    expr = "SET #s = :to, updated_at = :t"
    names = {"#s": "status"}
    values: dict[str, Any] = {":to": to.value, ":t": int(time.time())}
 
    # for i, (key, value) in enumerate(fields.items()):
    #     expr += f", #f{i} = :v{i}"
    #     names[f"#f{i}"] = key
    #     values[f":v{i}"] = value
    for i, (key, value) in enumerate(fields.items()):
        expr += f", #f{i} = :v{i}"
        names[f"#f{i}"] = key
        values[f":v{i}"] = _to_ddb(value)
 
    placeholders = []
    for i, s in enumerate(legal_from):
        values[f":from{i}"] = s
        placeholders.append(f":from{i}")
    condition = f"attribute_exists(pk) AND #s IN ({', '.join(placeholders)})"
 
    try:
        resp = _table.update_item(
            Key={"pk": f"INCIDENT#{incident_id}"},
            UpdateExpression=expr,
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            current = get_incident(incident_id)
            raise IllegalTransition(
                f"{incident_id}: cannot move to {to} from "
                f"{current.get('status') if current else 'MISSING'}"
            ) from exc
        raise
    return _clean(resp["Attributes"])
 
 
def list_by_status(status: Status, limit: int = 50) -> list[dict]:
    resp = _table.scan(
        FilterExpression="#s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status.value},
        Limit=limit,
    )
    return [_clean(i) for i in resp.get("Items", [])]