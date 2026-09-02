"""Agent worker — the investigation.

    SQS work queue -> [this] -> Bedrock agent loop -> validated RCA -> DynamoDB

Status routing after the RCA is produced:
    needs_human_investigation      -> ESCALATED       (no fix proposed)
    remediation == none            -> INFORMATIONAL   (real cause, nothing to automate)
    otherwise                      -> PENDING_APPROVAL (a human decides)

Failure of the agent itself is not silent: the incident goes to ESCALATED with
the error recorded, because a human still needs to look at the original alarm.
"""

import json

from .bedrock import (
    AgentRun,
    MaxStepsExceeded,
    ModelAccessError,
    extract_json,
    run_agent,
)
from ..common.incidents import Status, get_incident, transition
from ..common.logging import get_logger, log_event
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .schema import InvalidRCA, RCA, Remediation, validate
from .tools.init import specs, dispatch
logger = get_logger("agent")

TOOLS = specs()
# Populated in SEN-16..19. With none registered the agent has no evidence and
# must escalate — which is exactly the abstention behaviour worth proving first.
# TOOLS: list[dict] = []


def execute_tool(name: str, tool_input: dict):
    raise NotImplementedError(f"tool '{name}' is not registered")


def handler(event: dict, context) -> dict:
    failures = []

    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            investigate(body["incident_id"])
        except Exception as exc:
            log_event(logger, "error", f"investigation failed: {exc}",
                      error_type=type(exc).__name__,
                      message_id=record.get("messageId"))
            logger.exception("investigation failed")
            failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": failures}


def investigate(incident_id: str) -> None:
    incident = get_incident(incident_id)
    if not incident:
        log_event(logger, "warning", "incident not found, dropping",
                  incident_id=incident_id)
        return

    if incident["status"] != Status.NEW.value:
        # Already handled — an SQS redelivery, not a new investigation.
        log_event(logger, "info", "incident already in progress, skipping",
                  incident_id=incident_id, status=incident["status"])
        return

    transition(incident_id, Status.INVESTIGATING)
    log_event(logger, "info", "investigation started",
              incident_id=incident_id, alarm_name=incident.get("alarm_name"))

    try:
        rca, run = _run_investigation(incident)
    except ModelAccessError as exc:
        # Configuration problem, not an incident problem. Loud and specific.
        log_event(logger, "error", f"bedrock unavailable: {exc}",
                  incident_id=incident_id)
        transition(incident_id, Status.ESCALATED,
                   escalation_reason=f"agent could not run: {exc}")
        raise
    except MaxStepsExceeded as exc:
        log_event(logger, "warning", "agent hit step limit without concluding",
                  incident_id=incident_id)
        transition(incident_id, Status.ESCALATED,
                   escalation_reason=str(exc))
        return
    except Exception as exc:
        transition(incident_id, Status.FAILED,
                   failure_reason=f"{type(exc).__name__}: {exc}")
        raise

    next_status = _route(rca)
    transition(
        incident_id,
        next_status,
        rca=rca.to_dict(),
        trace=run.to_dict(),
        cost_usd=str(round(run.cost_usd, 6)),
        confidence=str(rca.confidence),
    )

    log_event(logger, "info", "investigation complete",
              incident_id=incident_id,
              root_cause=rca.root_cause_category,
              confidence=rca.confidence,
              suspect_change=rca.suspect_change,
              remediation=rca.proposed_remediation,
              next_status=next_status.value,
              steps=len(run.steps),
              cost_usd=round(run.cost_usd, 6))


def _run_investigation(incident: dict) -> tuple[RCA, AgentRun]:
    """Run the agent, then validate. One repair attempt on invalid output."""
    run = run_agent(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(incident),
        tools=TOOLS,
        executor=lambda name, params: dispatch(name, params, incident),
    )

    try:
        return validate(extract_json(run.final_text)), run
    except (InvalidRCA, ValueError) as first_error:
        # log_event(logger, "warning", "invalid RCA, attempting repair",
        #           error=str(first_error))
        log_event(logger, "warning", "invalid RCA, attempting repair",
                  error=str(first_error), raw=run.final_text[:800])
        if not run.final_text.strip():
            raise MaxStepsExceeded("model produced no output to repair")
        repair = run_agent(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=(
                f"You previously produced this analysis:\n\n{run.final_text}\n\n"
                f"It was rejected: {first_error}\n"
                f"Return the corrected JSON object only. Do not investigate further."
            ),
            tools=[],
            executor=lambda name, params: dispatch(name, params, incident),
        )
        # Fold the repair's cost into the run so the metric stays honest.
        run.input_tokens += repair.input_tokens
        run.output_tokens += repair.output_tokens
        run.cache_read_tokens += repair.cache_read_tokens
        run.cache_write_tokens += repair.cache_write_tokens
        run.final_text = repair.final_text
        run.steps.extend(repair.steps)

        return validate(extract_json(repair.final_text)), run


def _route(rca: RCA) -> Status:
    if rca.needs_human_investigation:
        return Status.ESCALATED
    if rca.proposed_remediation == Remediation.NONE.value:
        return Status.INFORMATIONAL
    return Status.PENDING_APPROVAL