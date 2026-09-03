"""Approval gate — where a human decides.

    GET    /incidents                 pending incidents, newest first
    GET    /incidents/{id}            one incident with its full RCA and trace
    POST   /incidents/{id}/approve    -> APPROVED, then invoke the executor
    POST   /incidents/{id}/reject     -> REJECTED

This is the only place a remediation can be authorised. It is deliberately a
separate function from both the agent (which may not write) and the executor
(which holds the write permissions): the gate can *authorise* an action and the
executor can *perform* one, but neither can do both alone.

The approve path does not perform the remediation itself. It transitions the
incident and invokes the executor, so the audit trail records who approved and
what was then done as two separate facts.
"""

import json
import os
from typing import Any

import boto3

from ..agent.config import Config
from ..common.incidents import (
    IllegalTransition,
    Status,
    get_incident,
    list_by_status,
    transition,
)
from ..common.logging import get_logger, log_event

logger = get_logger("approval")

_lambda = boto3.client("lambda", region_name=Config.REGION)

EXECUTOR_FUNCTION = os.environ.get("EXECUTOR_FUNCTION", "")
# Shared secret. Not an identity system — this is a capstone, and the honest
# framing is that it stops an accidental request, not a determined attacker.
# Anything real would put an authorizer in front of this.
APPROVAL_TOKEN = os.environ.get("APPROVAL_TOKEN", "")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _route(event: dict) -> tuple[str, str]:
    ctx = event.get("requestContext", {}).get("http", {})
    return ctx.get("method", "GET"), event.get("rawPath", "/")


def _authorised(event: dict) -> bool:
    if not APPROVAL_TOKEN:
        # Fail closed. An unset token means misconfiguration, and the safe
        # reading of "no credential configured" is "nobody may approve".
        log_event(logger, "error", "APPROVAL_TOKEN is not set; refusing all requests")
        return False
    return event.get("headers", {}).get("x-approval-token") == APPROVAL_TOKEN


def _summarise(incident: dict) -> dict:
    """What an operator needs to decide, without the full trace."""
    rca = incident.get("rca") or {}
    return {
        "incident_id": incident.get("incident_id"),
        "status": incident.get("status"),
        "alarm_name": incident.get("alarm_name"),
        "triggered_at": incident.get("triggered_at"),
        "root_cause": rca.get("root_cause_category"),
        "summary": rca.get("summary"),
        "confidence": rca.get("confidence"),
        "suspect_change": rca.get("suspect_change"),
        "affected_component": rca.get("affected_component"),
        "proposed_remediation": rca.get("proposed_remediation"),
        "remediation_detail": rca.get("remediation_detail"),
        "cost_usd": incident.get("cost_usd"),
    }


def _approve(incident_id: str, actor: str) -> dict:
    incident = get_incident(incident_id)
    if not incident:
        return _response(404, {"error": f"no such incident: {incident_id}"})

    try:
        # The conditional write is the guard: two operators approving the same
        # incident cannot both succeed, so the executor cannot run twice.
        transition(incident_id, Status.APPROVED, approved_by=actor)
    except IllegalTransition as exc:
        return _response(409, {"error": str(exc),
                               "current_status": incident.get("status")})

    log_event(logger, "info", "incident approved",
              incident_id=incident_id, approved_by=actor,
              remediation=(incident.get("rca") or {}).get("proposed_remediation"))

    if not EXECUTOR_FUNCTION:
        return _response(200, {
            "incident_id": incident_id,
            "status": Status.APPROVED.value,
            "warning": "approved, but EXECUTOR_FUNCTION is unset so nothing ran",
        })

    # Asynchronous: a rollback can outlast an HTTP request, and the operator
    # does not need to hold the connection open to learn it was authorised.
    # The incident record carries the outcome.
    _lambda.invoke(
        FunctionName=EXECUTOR_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps({"incident_id": incident_id}).encode(),
    )

    return _response(202, {
        "incident_id": incident_id,
        "status": Status.APPROVED.value,
        "executor_invoked": True,
    })


def _reject(incident_id: str, actor: str, reason: str | None) -> dict:
    incident = get_incident(incident_id)
    if not incident:
        return _response(404, {"error": f"no such incident: {incident_id}"})

    try:
        transition(incident_id, Status.REJECTED,
                   rejected_by=actor, rejection_reason=reason or "")
    except IllegalTransition as exc:
        return _response(409, {"error": str(exc),
                               "current_status": incident.get("status")})

    log_event(logger, "info", "incident rejected",
              incident_id=incident_id, rejected_by=actor, reason=reason)
    return _response(200, {"incident_id": incident_id,
                           "status": Status.REJECTED.value})


def handler(event: dict, context) -> dict:
    method, path = _route(event)

    if not _authorised(event):
        return _response(403, {"error": "forbidden"})

    body: dict[str, Any] = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except json.JSONDecodeError:
            return _response(400, {"error": "body is not valid JSON"})

    actor = body.get("actor") or event.get("headers", {}).get("x-actor") or "unknown"

    if method == "GET" and path.rstrip("/") == "/incidents":
        pending = list_by_status(Status.PENDING_APPROVAL)
        pending.sort(key=lambda i: i.get("triggered_at") or 0, reverse=True)
        return _response(200, {"count": len(pending),
                               "incidents": [_summarise(i) for i in pending]})

    if path.startswith("/incidents/"):
        rest = path.removeprefix("/incidents/").rstrip("/")

        if method == "POST" and rest.endswith("/approve"):
            return _approve(rest.removesuffix("/approve"), actor)

        if method == "POST" and rest.endswith("/reject"):
            return _reject(rest.removesuffix("/reject"), actor, body.get("reason"))

        if method == "GET" and "/" not in rest:
            incident = get_incident(rest)
            if not incident:
                return _response(404, {"error": f"no such incident: {rest}"})
            # Full record here, including the trace — this is the view an
            # operator uses to check the agent's reasoning before approving.
            return _response(200, incident)

    return _response(404, {"error": "not found", "path": path})
