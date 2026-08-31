"""SEN-16 — logs tool. The agent's primary evidence source.

CloudWatch Logs Insights is asynchronous: StartQuery returns a queryId, then you
poll GetQueryResults until the status leaves Running/Scheduled.

Two things worth getting right:
  * ingestion lag — logs can trail the alarm by up to a minute, so an empty
    first result is not proof of no errors. Retry once before concluding.
  * `truncated` — "I saw 50 of possibly many" is different evidence from
    "I saw all 12". Tell the model which it got.
"""

import time

import boto3

from ..config import Config
from ...common.logging import get_logger, log_event
from .base import error_result, resolve_log_groups, truncate, window_for

logger = get_logger("tool.logs")
_logs = boto3.client("logs", region_name=Config.REGION)

POLL_INTERVAL_S = 1.0
POLL_TIMEOUT_S = 20
EMPTY_RETRY_SLEEP_S = 3
TERMINAL_STATUSES = ("Complete", "Failed", "Cancelled", "Timeout", "Unknown")


TOOL_SPEC = {
    "toolSpec": {
        "name": "search_logs",
        # This text is the ONLY thing the model sees when choosing tools.
        "description": (
            "Search the target application's logs around the time of the incident. "
            "Returns structured log entries including level, message, error type, "
            "stack trace and correlation id. Use this first to find out what "
            "actually failed. The time window is fixed to the incident window; "
            "you cannot widen it."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "log_group": {
                        "type": "string",
                        "enum": ["api", "consumer", "both"],
                        "description": (
                            "Which service's logs to search. 'api' is the request "
                            "handler, 'consumer' processes queued messages. Use "
                            "'both' when you do not yet know where the fault is."
                        ),
                    },
                    "level": {
                        "type": "string",
                        "enum": ["ERROR", "WARNING", "all"],
                        "description": "Filter by log level. Default 'all'.",
                    },
                    "search_term": {
                        "type": "string",
                        "description": (
                            "Optional text to match in the message, e.g. an order id "
                            "or correlation id, to trace one request across services."
                        ),
                    },
                },
                "required": ["log_group"],
            }
        },
    }
}


def _escape(term: str) -> str:
    """Insights regex literals are /slash delimited/."""
    return term.replace("\\", "\\\\").replace("/", r"\/")


def _build_query(level: str | None, search_term: str | None) -> str:
    """Compose the query ourselves. Never accept a query string from the model."""
    parts = [
        "fields @timestamp, level, service, message, correlation_id, "
        "error_type, stack_trace, order_id"
    ]
    if level and level != "all":
        parts.append(f"filter level = '{level}'")
    if search_term:
        parts.append(f"filter @message like /{_escape(search_term)}/")
    parts.append("sort @timestamp desc")
    parts.append(f"limit {Config.LOG_QUERY_LIMIT}")
    return " | ".join(parts)


def _flatten(results: list[list[dict]]) -> list[dict]:
    """Insights returns each row as [{'field': 'x', 'value': 'y'}, ...]."""
    rows = []
    for raw_row in results:
        row = {
            item["field"]: item["value"]
            for item in raw_row
            if item.get("field") != "@ptr"          # internal pointer, no value to the model
        }
        if "stack_trace" in row:
            row["stack_trace"] = truncate(row["stack_trace"])
        if "message" in row:
            row["message"] = truncate(row["message"], 300)
        rows.append(row)
    return rows


def _dedupe(rows: list[dict]) -> list[dict]:
    """Collapse identical failures. 16 copies of the same KeyError is one fact,
    not sixteen — and it is resent on every subsequent turn."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        trace = row.get("stack_trace") or ""
        key = (row.get("level"), row.get("error_type"),
               row.get("message"), trace[:200])
        if key in groups:
            g = groups[key]
            g["occurrences"] += 1
            g["last_seen"] = row.get("@timestamp")
        else:
            g = dict(row)
            g["occurrences"] = 1
            g["first_seen"] = row.get("@timestamp")
            g["last_seen"] = row.get("@timestamp")
            groups[key] = g
    return list(groups.values())


def _run_query(log_groups: list[str], query: str,
               start: int, end: int) -> tuple[list[dict], str]:
    """Start a query and poll to completion.

    Returns (rows, status). The status matters as evidence in its own right —
    a 'Timeout' tells the model something different from an empty 'Complete'.
    """
    started = _logs.start_query(
        logGroupNames=log_groups,
        startTime=start,
        endTime=end,
        queryString=query,
        limit=Config.LOG_QUERY_LIMIT,
    )
    query_id = started["queryId"]

    deadline = time.time() + POLL_TIMEOUT_S
    status = "Running"
    results: list[list[dict]] = []

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        resp = _logs.get_query_results(queryId=query_id)
        status = resp.get("status", "Unknown")
        results = resp.get("results", [])
        if status in TERMINAL_STATUSES:
            break
    else:
        # Ran out of time — stop the query so it does not keep scanning.
        try:
            _logs.stop_query(queryId=query_id)
        except Exception:
            pass
        status = "Timeout"

    return _flatten(results), status


def run(incident: dict, log_group: str = "both",
        level: str = "all", search_term: str | None = None) -> dict:
    """Entry point the executor calls."""
    try:
        groups = resolve_log_groups(log_group)
        start, end = window_for(incident)
    except Exception as exc:
        return error_result(str(exc))

    query = _build_query(level, search_term)

    try:
        rows, status = _run_query(groups, query, start, end)

        # Ingestion lag: an empty first result is not proof of no errors.
        if not rows and status == "Complete":
            log_event(logger, "info", "no rows, retrying once for ingestion lag")
            time.sleep(EMPTY_RETRY_SLEEP_S)
            rows, status = _run_query(groups, query, start, end)

    except Exception as exc:
        log_event(logger, "warning", f"log query failed: {exc}",
                  error_type=type(exc).__name__)
        return error_result(f"log search failed: {exc}", log_groups_searched=groups)

    log_event(logger, "info", "log search complete",
              groups=groups, rows=len(rows), status=status)
    unique = _dedupe(rows)
    return {
        "log_groups_searched": groups,
        "window": {"start": start, "end": end,
                   "minutes_each_side": Config.LOG_WINDOW_MINUTES},
        "query_status": status,
        "result_count": len(rows),
        "unique_patterns": len(unique),
        "truncated": len(rows) >= Config.LOG_QUERY_LIMIT,
        "entries": rows,
    }