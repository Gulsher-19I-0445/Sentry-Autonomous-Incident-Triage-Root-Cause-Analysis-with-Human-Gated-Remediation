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
            "you cannot widen it. "
            "Identical entries are collapsed into one pattern: `occurrences` is "
            "how many log lines that pattern stands for, and `first_seen` / "
            "`last_seen` bound when it happened. Read `occurrences`, not the "
            "number of entries, as the error count."
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
    # @message is the raw line. Without it the runtime's own output — the
    # REPORT line carrying "Error Type: Runtime.OutOfMemory", a timeout, a
    # segfault — arrives as a row of empty fields, because those lines are not
    # JSON and none of the named fields parse. A memory kill produces no
    # application log at all (the process is killed before the handler can log),
    # so this is the only way the agent can see one. _flatten drops it again for
    # rows that did parse, so structured entries are not sent twice.
    parts = [
        "fields @timestamp, @message, level, service, message, correlation_id, "
        "error_type, stack_trace, order_id"
    ]
    if level and level != "all":
        parts.append(f"filter level = '{level}'")
    if search_term:
        parts.append(f"filter @message like /{_escape(search_term)}/")
    parts.append("sort @timestamp desc")
    parts.append(f"limit {Config.LOG_QUERY_LIMIT}")
    return " | ".join(parts)


# A platform line is only evidence when it reports a failure. START, END and a
# routine REPORT say nothing the metrics tool does not say better, and they are
# emitted for EVERY invocation — so keeping them crowds real errors out of the
# result limit and invites the model to quote request ids back as findings.
RUNTIME_FAILURE_MARKERS = (
    "Runtime.OutOfMemory",
    "Task timed out",
    "Runtime exited",
    "Status: error",
    "errorType",
    "Segmentation fault",
)


def _is_runtime_failure(raw: str) -> bool:
    return any(marker in raw for marker in RUNTIME_FAILURE_MARKERS)


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

        # A row that parsed as JSON already carries everything @message holds,
        # so keeping both would send each entry twice — on every turn.
        if "@message" in row:
            if row.get("level"):
                row.pop("@message")
            elif _is_runtime_failure(row["@message"]):
                # The runtime's own account of a failure it killed. For an OOM
                # this is the ONLY record: the process dies before the handler
                # can log anything.
                row["@message"] = truncate(row["@message"], 300)
            else:
                # Routine platform chatter. Dropping the row entirely rather
                # than just the field, because a row with no parsed fields and
                # no failure marker carries nothing at all.
                continue

        rows.append(row)
    return rows


MAX_CORRELATION_SAMPLES = 3


def _dedupe(rows: list[dict]) -> list[dict]:
    """Collapse identical failures. 16 copies of the same KeyError is one fact,
    not sixteen — and it is resent on every subsequent turn.

    The collapsed group carries strictly more than the copies did: `occurrences`
    plus a first/last timestamp is the count and the time span, which the model
    would otherwise have to derive by counting rows (badly). Only the individual
    timestamps of repeat occurrences are lost.
    """
    groups: dict[tuple, dict] = {}
    for row in rows:
        trace = row.get("stack_trace") or ""
        # Platform lines have none of the JSON fields, so without @message in
        # the key every runtime line — START, END, REPORT, an OOM kill — would
        # collapse into a single indistinguishable group.
        key = (row.get("level"), row.get("error_type"),
               row.get("message"), trace[:200], row.get("@message"))
        if key in groups:
            g = groups[key]
            g["occurrences"] += 1
            # min/max rather than "first row wins", so the span stays correct
            # regardless of the query's sort direction. The Insights timestamp
            # format sorts correctly as a string.
            seen = row.get("@timestamp")
            if seen:
                if not g["first_seen"] or seen < g["first_seen"]:
                    g["first_seen"] = seen
                if not g["last_seen"] or seen > g["last_seen"]:
                    g["last_seen"] = seen
        else:
            g = dict(row)
            # Redundant once first_seen/last_seen exist, and it is one more
            # field resent on every turn.
            g.pop("@timestamp", None)
            g["occurrences"] = 1
            g["first_seen"] = row.get("@timestamp")
            g["last_seen"] = row.get("@timestamp")
            groups[key] = g

        # A couple of ids are enough to trace this failure across services; the
        # other thirteen are pure payload.
        for field in ("correlation_id", "order_id"):
            value = row.get(field)
            if not value:
                continue
            samples = g.setdefault(f"{field}s", [])
            if value not in samples and len(samples) < MAX_CORRELATION_SAMPLES:
                samples.append(value)

    for g in groups.values():
        # With one distinct value the plural list just repeats the singular.
        for field in ("correlation_id", "order_id"):
            plural = g.get(f"{field}s")
            if plural is not None and len(plural) <= 1:
                g.pop(f"{field}s")

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


def _level_census(log_groups: list[str], start: int, end: int) -> dict | None:
    """Count entries by level across the window, ignoring any filter.

    Run only when a filtered search came back empty. "0 ERROR, 240 INFO" is a
    finding; an empty result with no context is a puzzle, and the model resolves
    puzzles by spending another turn. One extra Insights query is far cheaper
    than the turn it saves, because every turn resends the whole transcript.
    """
    try:
        rows, status = _run_query(
            log_groups, "stats count(*) as entries by level", start, end
        )
    except Exception as exc:
        log_event(logger, "warning", f"level census failed: {exc}")
        return None

    if status != "Complete":
        return None

    census: dict[str, int] = {}
    for row in rows:
        level = row.get("level") or "UNKNOWN"
        try:
            census[level] = int(float(row.get("entries", 0)))
        except (TypeError, ValueError):
            continue
    return census


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

    unique = _dedupe(rows)

    log_event(logger, "info", "log search complete",
              groups=groups, rows=len(rows), patterns=len(unique), status=status)

    levels: dict[str, int] = {}
    for entry in unique:
        level = entry.get("level") or "UNKNOWN"
        levels[level] = levels.get(level, 0) + entry["occurrences"]

    payload = {
        "log_groups_searched": groups,
        "window": {"start": start, "end": end,
                   "minutes_each_side": Config.LOG_WINDOW_MINUTES},
        "query_status": status,
        "result_count": len(rows),
        "unique_patterns": len(unique),
        "level_counts": levels,
        "truncated": len(rows) >= Config.LOG_QUERY_LIMIT,
        # Deduplicated. `occurrences` is how many raw lines each pattern stands
        # for, so nothing about frequency is lost — but the identical copies are
        # not resent on every subsequent turn of the Converse loop.
        "entries": unique,
    }

    # An empty ERROR search is ambiguous on its own: no errors, or a filter that
    # missed? Answer it here rather than letting the model spend a turn re-asking.
    if not rows and level and level != "all":
        census = _level_census(groups, start, end)
        if census is not None:
            payload["window_level_counts"] = census
            payload["note"] = (
                f"No entries matched level={level}. `window_level_counts` shows "
                f"every level present in this window, so an empty result means "
                f"there were none of that level — not that the search failed. "
                f"If the alarm was for latency, throttling or queue depth, the "
                f"evidence is in metrics rather than logs."
            )

    return payload