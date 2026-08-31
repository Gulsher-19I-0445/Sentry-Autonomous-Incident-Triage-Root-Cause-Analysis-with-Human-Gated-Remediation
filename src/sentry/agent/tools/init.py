"""Tool registry.

The handler imports specs() and dispatch() from here, so adding a tool is a
one-line change in this file rather than an edit to the handler.

Registration is also the test lever: an unregistered tool does not exist to the
model, so you can run the agent with a subset — or with none, which is how the
abstention behaviour was proved before any tool existed.
"""

from typing import Any, Callable

from ...common.logging import get_logger, log_event
from . import changes, logs, metrics, runbooks

logger = get_logger("tools")

# name -> (toolSpec, callable). Callables take (incident, **params).
_REGISTRY: dict[str, tuple[dict, Callable[..., dict]]] = {
    "search_logs": (logs.TOOL_SPEC, logs.run),
    "get_metrics": (metrics.TOOL_SPEC, metrics.run),
    "get_recent_changes": (changes.TOOL_SPEC, changes.run_changes),
    "search_runbooks": (runbooks.TOOL_SPEC, runbooks.run),
}


def specs() -> list[dict]:
    """The Converse toolConfig list."""
    return [spec for spec, _ in _REGISTRY.values()]


def names() -> list[str]:
    return sorted(_REGISTRY)


def dispatch(name: str, tool_input: dict, incident: dict) -> Any:
    """Route a tool call.

    An unknown tool or a bad argument is returned to the model as an error
    result, not raised: the model can correct itself, and a malformed call
    should not abort an investigation.
    """
    entry = _REGISTRY.get(name)
    if not entry:
        log_event(logger, "warning", "unknown tool requested", tool=name)
        return {"error": f"unknown tool '{name}'. Available: {names()}"}

    _, fn = entry
    try:
        return fn(incident, **tool_input)
    except TypeError as exc:
        # Wrong or missing arguments — the model can fix this on the next turn.
        log_event(logger, "warning", "bad tool arguments",
                  tool=name, args=list(tool_input), error=str(exc))
        return {"error": f"invalid arguments for '{name}': {exc}"}