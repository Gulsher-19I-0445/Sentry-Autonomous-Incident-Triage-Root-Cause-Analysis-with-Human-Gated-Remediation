"""Ingest Lambda — alarm to incident.

    CloudWatch Alarm -> SNS -> [this] -> DynamoDB (dedup) -> SQS work queue

Three jobs, in order:
  1. Ignore anything that is not a transition INTO the ALARM state. Recoveries
     and INSUFFICIENT_DATA are noise; investigating them wastes tokens.
  2. Deduplicate. A burst of errors makes an alarm flap OK->ALARM->OK->ALARM,
     and each transition publishes to SNS. Without this, one incident becomes
     four investigations at ~$0.04 each.
  3. Enqueue exactly one work item per real incident.
"""

import json
import os
import time

import boto3

from ..common.incidents import (
    DuplicateIncident,
    build_incident_id,
    create_incident,
    record_suppression,
)
from ..common.logging import get_logger, log_event

logger = get_logger("ingest")
sqs = boto3.client("sqs")

WORK_QUEUE_URL = os.environ["WORK_QUEUE_URL"]


def _parse_timestamp(raw: str | None) -> int:
    """CloudWatch sends e.g. '2026-08-24T09:13:00.000+0000'."""
    if not raw:
        return int(time.time())
    try:
        cleaned = raw.replace("Z", "+0000")
        return int(time.mktime(time.strptime(cleaned.split(".")[0], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return int(time.time())


def handler(event: dict, context) -> dict:
    processed, suppressed, ignored = 0, 0, 0

    for record in event.get("Records", []):
        try:
            outcome = _handle_record(record)
        except Exception as exc:
            log_event(logger, "error", f"ingest failed: {exc}",
                      error_type=type(exc).__name__)
            logger.exception("ingest failed")
            raise  # let SNS retry; a lost alarm is worse than a duplicate

        if outcome == "processed":
            processed += 1
        elif outcome == "suppressed":
            suppressed += 1
        else:
            ignored += 1

    log_event(logger, "info", "ingest batch complete",
              processed=processed, suppressed=suppressed, ignored=ignored)
    return {"processed": processed, "suppressed": suppressed, "ignored": ignored}


def _handle_record(record: dict) -> str:
    # SNS wraps the alarm payload as a JSON string inside a JSON envelope.
    raw = record.get("Sns", {}).get("Message", "{}")
    try:
        alarm = json.loads(raw)
    except json.JSONDecodeError:
        log_event(logger, "warning", "non-JSON SNS message ignored")
        return "ignored"

    alarm_name = alarm.get("AlarmName")
    new_state = alarm.get("NewStateValue")

    if not alarm_name:
        log_event(logger, "warning", "message is not a CloudWatch alarm, ignoring")
        return "ignored"

    # Only investigate transitions INTO alarm.
    if new_state != "ALARM":
        log_event(logger, "info", "non-ALARM transition ignored",
                  alarm_name=alarm_name, new_state=new_state,
                  old_state=alarm.get("OldStateValue"))
        return "ignored"

    triggered_at = _parse_timestamp(alarm.get("StateChangeTime"))
    alarm["_triggered_at"] = triggered_at
    incident_id = build_incident_id(alarm_name, triggered_at)

    try:
        incident = create_incident(incident_id, alarm)
    except DuplicateIncident:
        count = record_suppression(incident_id)
        log_event(logger, "info", "duplicate alarm suppressed",
                  incident_id=incident_id, alarm_name=alarm_name,
                  suppressed_count=count)
        return "suppressed"

    sqs.send_message(
        QueueUrl=WORK_QUEUE_URL,
        MessageBody=json.dumps({
            "incident_id": incident_id,
            "alarm_name": alarm_name,
            "triggered_at": triggered_at,
        }),
    )

    log_event(logger, "info", "incident created and queued",
              incident_id=incident_id,
              alarm_name=alarm_name,
              metric=incident.get("metric_name"),
              state_reason=incident.get("state_reason"))
    return "processed"