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
    list_all,
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


# The dashboard is a static page served from somewhere else (a file:// URL
# during development), so every response needs CORS or the browser discards it
# before any JS runs. The token still gates access — CORS is not a security
# boundary, it just decides whose JavaScript may read the reply.
CORS_HEADERS = {
    "access-control-allow-origin": os.environ.get("ALLOWED_ORIGIN", "*"),
    "access-control-allow-headers": "content-type,x-approval-token,x-actor",
    "access-control-allow-methods": "GET,POST,OPTIONS",
}


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **CORS_HEADERS},
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
        # For an incident still being worked, these are all the dashboard has —
        # there is no RCA yet. updated_at is what makes a stuck INVESTIGATING
        # distinguishable from one that is simply still running.
        "updated_at": incident.get("updated_at"),
        "state_reason": incident.get("state_reason"),
        "suppressed_count": incident.get("suppressed_count"),
        "root_cause": rca.get("root_cause_category"),
        "summary": rca.get("summary"),
        "confidence": rca.get("confidence"),
        "suspect_change": rca.get("suspect_change"),
        "affected_component": rca.get("affected_component"),
        "proposed_remediation": rca.get("proposed_remediation"),
        "remediation_detail": rca.get("remediation_detail"),
        "needs_human_investigation": rca.get("needs_human_investigation"),
        "runbook_applied": rca.get("runbook_applied"),
        "evidence": rca.get("evidence") or [],
        "cost_usd": incident.get("cost_usd"),
        "tool_calls": len((incident.get("trace") or {}).get("steps") or []),
        # Only present once a human has acted; the dashboard uses them to show
        # who did what rather than just a status word.
        "approved_by": incident.get("approved_by"),
        "rejected_by": incident.get("rejected_by"),
        "rejection_reason": incident.get("rejection_reason"),
        "execution_result": incident.get("execution_result"),
        "escalation_reason": incident.get("escalation_reason"),
        "failure_reason": incident.get("failure_reason"),
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


def _dashboard_view(status_filter: str) -> dict:
    """Incidents for the dashboard, newest first.

    Two paths on purpose. `all` — what the board always asks for — is one
    unfiltered scan grouped in code; a scan per status meant ten per refresh,
    about 120 a minute with auto-refresh on. It also paginates, so a table
    larger than the scan limit does not silently drop incidents.

    An explicit status list still uses the filtered scan, which is cheaper when
    only one status is wanted.
    """
    if status_filter.lower() == "all":
        incidents = list_all()
    else:
        wanted = {s.strip().upper() for s in status_filter.split(",") if s.strip()}
        statuses = [s for s in Status if s.value in wanted]
        if not statuses:
            return {"error": f"unknown status {status_filter!r}",
                    "valid": [s.value for s in Status]}

        incidents = []
        for status in statuses:
            incidents.extend(list_by_status(status))

    incidents.sort(key=lambda i: i.get("triggered_at") or 0, reverse=True)

    counts: dict[str, int] = {}
    for incident in incidents:
        key = incident.get("status") or "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1

    return {
        "count": len(incidents),
        "counts_by_status": counts,
        "incidents": [_summarise(i) for i in incidents],
    }


def handler(event: dict, context) -> dict:
    method, path = _route(event)

    # Preflight carries no auth header by design — answering it is not access.
    if method == "OPTIONS":
        return _response(204, {})

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
        params = event.get("queryStringParameters") or {}
        # Defaults to the approval queue: the common case is "what needs me?",
        # and the dashboard asks for `all` explicitly when it wants history.
        view = _dashboard_view(params.get("status") or Status.PENDING_APPROVAL.value)
        return _response(400 if "error" in view else 200, view)

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
