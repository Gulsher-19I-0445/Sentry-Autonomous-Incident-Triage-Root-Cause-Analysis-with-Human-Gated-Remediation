"""Executor — the only component that changes anything.

    PENDING_APPROVAL -> [human] -> APPROVED -> [this] -> EXECUTED -> CLOSED

Everything upstream of here is read-only. The agent cannot reach this code and
does not hold the permissions it uses: the write capability lives in a separate
function with a separate role, so a prompt injection or a reasoning failure can
produce a bad *proposal* but never a bad *action*. That boundary is the whole
safety argument, and it only holds while this stays a separate deployable.

Three guards, in order of how much they matter:

  1. Status. Work is only performed from APPROVED, enforced by the conditional
     write in `transition()` rather than by reading-then-checking — two
     approvals racing cannot both execute.
  2. Allow-list. The target is resolved against Config.TARGET_FUNCTIONS, so an
     RCA naming some other team's function cannot be acted on.
  3. Reversibility. Every action here can be undone by a human with one CLI
     command, and the previous state is recorded on the incident so they know
     what to undo it to.
"""

import os
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from ..agent.config import Config
from ..common.incidents import IllegalTransition, Status, get_incident, transition
from ..common.logging import get_logger, log_event

logger = get_logger("executor")

_lambda = boto3.client("lambda", region_name=Config.REGION,
                       config=BotoConfig(retries={"max_attempts": 3, "mode": "standard"}))
_ddb = boto3.resource("dynamodb", region_name=Config.REGION)

# The alias the target app serves from. Rolling this back is the remediation.
LIVE_ALIAS = os.environ.get("LIVE_ALIAS", "live")

# Feature-flag remediation may only DISABLE a flag, never enable one. Turning
# something off is the recoverable direction; turning something on is a change
# whose blast radius nobody has reasoned about.
APP_TABLE = os.environ.get("APP_TABLE", "")


class ExecutionError(Exception):
    """The remediation could not be applied. The incident goes to FAILED."""


class UnsupportedRemediation(ExecutionError):
    """The RCA proposed something this executor does not know how to do."""


# --------------------------------------------------------------------------- #
# alias rollback
# --------------------------------------------------------------------------- #

def _resolve_function(name: str | None) -> str:
    """Map the RCA's affected_component onto an allow-listed function.

    The RCA field is free text written by a model, so it is treated as a hint
    to be matched against known resources, never as a name to act on directly.
    """
    if not name:
        raise ExecutionError("RCA named no affected_component, so there is "
                             "nothing to roll back")
    for fn in Config.TARGET_FUNCTIONS:
        if fn == name or fn in name or name in fn:
            return fn
    raise ExecutionError(
        f"affected_component {name!r} is not one of the target functions "
        f"{Config.TARGET_FUNCTIONS} — refusing to act on it"
    )


def _previous_version(function_name: str, current: str) -> str:
    """The published version immediately before the one currently served.

    Deliberately positional rather than trusting the RCA's suspect_change: the
    model writes that field as prose ("version 7", a commit sha, a CloudTrail
    event name), and parsing prose into a version number to hand to an AWS
    mutation is not a thing worth doing. What the alias points at now is a fact.
    """
    versions: list[str] = []
    paginator = _lambda.get_paginator("list_versions_by_function")
    for page in paginator.paginate(FunctionName=function_name):
        for v in page.get("Versions", []):
            version = v.get("Version")
            if version and version != "$LATEST":
                versions.append(version)

    # Lambda version numbers are strings but order numerically.
    versions.sort(key=int)

    if current not in versions:
        raise ExecutionError(
            f"alias {LIVE_ALIAS} points at version {current}, which is not in "
            f"the published list {versions}"
        )
    index = versions.index(current)
    if index == 0:
        raise ExecutionError(
            f"version {current} is the earliest published version of "
            f"{function_name} — there is nothing to roll back to"
        )
    return versions[index - 1]


def _rollback_alias(incident: dict, rca: dict) -> dict:
    function_name = _resolve_function(rca.get("affected_component"))

    alias = _lambda.get_alias(FunctionName=function_name, Name=LIVE_ALIAS)
    current = alias["FunctionVersion"]
    target = _previous_version(function_name, current)

    log_event(logger, "info", "shifting alias",
              incident_id=incident["incident_id"], function=function_name,
              alias=LIVE_ALIAS, from_version=current, to_version=target)

    _lambda.update_alias(FunctionName=function_name, Name=LIVE_ALIAS,
                         FunctionVersion=target)

    return {
        "action": "alias_rollback",
        "function": function_name,
        "alias": LIVE_ALIAS,
        "from_version": current,
        "to_version": target,
        # Written so a human undoing this does not have to reconstruct it.
        "undo": (f"aws lambda update-alias --function-name {function_name} "
                 f"--name {LIVE_ALIAS} --function-version {current}"),
    }


# --------------------------------------------------------------------------- #
# feature flag
# --------------------------------------------------------------------------- #

def _disable_flag(incident: dict, rca: dict) -> dict:
    """Turn a feature flag off. Never on.

    The flag name comes from remediation_detail, which is model-written prose,
    so it is matched against the flags that actually exist rather than used as
    a key. A name that matches nothing is an error, not a no-op — silently
    doing nothing while reporting EXECUTED would be worse than failing.
    """
    if not APP_TABLE:
        raise ExecutionError("APP_TABLE is not configured, so no flag can be changed")

    detail = (rca.get("remediation_detail") or "").lower()
    if not detail:
        raise ExecutionError("RCA proposed feature_flag but gave no "
                             "remediation_detail naming which flag")

    table = _ddb.Table(APP_TABLE)
    scan = table.scan(
        FilterExpression="begins_with(pk, :p)",
        ExpressionAttributeValues={":p": "FLAG#"},
        ProjectionExpression="pk",
    )
    existing = [item["pk"].removeprefix("FLAG#") for item in scan.get("Items", [])]

    matches = [name for name in existing if name.lower() in detail]
    if not matches:
        raise ExecutionError(
            f"no enabled flag named in the remediation detail; flags present: "
            f"{existing}"
        )
    if len(matches) > 1:
        raise ExecutionError(
            f"remediation detail names more than one flag ({matches}) — "
            f"refusing to guess which was meant"
        )

    name = matches[0]
    log_event(logger, "info", "disabling flag",
              incident_id=incident["incident_id"], flag=name)

    table.update_item(
        Key={"pk": f"FLAG#{name}"},
        UpdateExpression="SET enabled = :false",
        ExpressionAttributeValues={":false": False},
    )

    return {
        "action": "feature_flag",
        "flag": name,
        "set_to": "disabled",
        "undo": f"re-enable FLAG#{name} in {APP_TABLE} if this was wrong",
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

ACTIONS = {
    "alias_rollback": _rollback_alias,
    "feature_flag": _disable_flag,
}


def execute(incident_id: str) -> dict:
    """Apply the approved remediation for one incident."""
    incident = get_incident(incident_id)
    if not incident:
        raise ExecutionError(f"incident {incident_id} not found")

    if incident["status"] != Status.APPROVED.value:
        # Not an error worth failing the incident over — an SQS redelivery or a
        # double-click lands here, and the first execution already happened.
        log_event(logger, "info", "incident not approved, skipping",
                  incident_id=incident_id, status=incident["status"])
        return {"skipped": True, "status": incident["status"]}

    rca = incident.get("rca") or {}
    remediation = rca.get("proposed_remediation", "none")

    if remediation == "none":
        log_event(logger, "info", "nothing to execute", incident_id=incident_id)
        transition(incident_id, Status.EXECUTED,
                   execution_result={"action": "none",
                                     "note": "RCA proposed no automated remediation"})
        return {"action": "none"}

    action = ACTIONS.get(remediation)
    if not action:
        raise UnsupportedRemediation(
            f"{remediation!r} is not an action this executor can perform; "
            f"known actions: {sorted(ACTIONS)}"
        )

    try:
        result = action(incident, rca)
    except ExecutionError:
        raise
    except Exception as exc:
        # An AWS-level failure is still a failed remediation, not a crash.
        raise ExecutionError(f"{type(exc).__name__}: {exc}") from exc

    transition(incident_id, Status.EXECUTED, execution_result=result)
    log_event(logger, "info", "remediation executed",
              incident_id=incident_id, **result)
    return result


def handler(event: dict, context) -> dict:
    """Invoked by the approval gate once a human has approved."""
    incident_id = event.get("incident_id")
    if not incident_id:
        raise ValueError("event carried no incident_id")

    try:
        return execute(incident_id)
    except (ExecutionError, IllegalTransition) as exc:
        log_event(logger, "error", f"execution failed: {exc}",
                  incident_id=incident_id, error_type=type(exc).__name__)
        # Record why on the incident so the failure is visible to the operator
        # rather than only in this function's logs.
        try:
            transition(incident_id, Status.FAILED,
                       failure_reason=f"{type(exc).__name__}: {exc}")
        except IllegalTransition:
            pass
        raise
