"""Shared plumbing for agent tools.

Conventions every tool follows:

  TOOL_SPEC   a Converse toolSpec dict. The `description` is the ONLY thing the
              model reads when deciding whether to call the tool, so it must say
              what the tool returns and when it is useful — not how it works.

  run(incident, **params) -> dict
              Always returns a dict. NEVER raises for an expected condition.
              "no results" and "access denied" are evidence the model should be
              able to reason about, not crashes.

Two guards apply to every tool:
  * the time window comes from the incident, never from the model — otherwise it
    will widen the search and your scan costs are unbounded
  * resource names are resolved against the Config allow-lists, never passed
    through — this is what keeps the agent out of other teams' resources in a
    shared account
"""

from typing import Any

from ..config import Config


class ToolError(Exception):
    """Only for genuine bugs. Expected failures return an error dict instead."""


def window_for(incident: dict, minutes: int | None = None) -> tuple[int, int]:
    """Incident time +/- the configured window, in unix seconds."""
    minutes = minutes or Config.LOG_WINDOW_MINUTES
    triggered = int(incident.get("triggered_at") or 0)
    if not triggered:
        raise ToolError("incident has no triggered_at timestamp")
    return triggered - minutes * 60, triggered + minutes * 60


def resolve_log_groups(choice: str) -> list[str]:
    """Map the model's short name onto real, allow-listed log groups.

    The model may only pick from these names. It cannot pass an arbitrary log
    group, so it cannot read anything outside the target app.
    """
    groups = Config.TARGET_LOG_GROUPS
    by_role = {}
    for g in groups:
        if g.endswith("api-gulsher"):
            by_role["api"] = g
        elif g.endswith("consumer-gulsher"):
            by_role["consumer"] = g

    if choice == "both":
        return list(groups)
    if choice in by_role:
        return [by_role[choice]]
    raise ToolError(f"unknown log group choice: {choice!r}")


def resolve_function(choice: str) -> str:
    """Same idea for Lambda function names."""
    for fn in Config.TARGET_FUNCTIONS:
        if fn.endswith(f"{choice}-gulsher"):
            return fn
    raise ToolError(f"unknown function choice: {choice!r}")


def error_result(message: str, **extra: Any) -> dict:
    """A failure the model should reason about rather than a crash."""
    return {"error": message, "result_count": 0, **extra}


def truncate(text: str | None, limit: int = 400) -> str | None:
    """Stack traces blow up token cost and the DynamoDB trace item."""
    if not text:
        return text
    return text if len(text) <= limit else text[:limit] + f"... [{len(text)} chars total]"