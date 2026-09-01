"""SEN-17 — metrics tool. Corroboration, and the load-vs-defect distinction.

This tool is what makes the 'pure load spike' decoy answerable. If errors rose
in proportion to invocations, the code is fine and the traffic changed. If
invocations were flat while errors rose, something broke. That distinction is
simply not present in the logs.

CloudWatch metrics have NO resource-level IAM, so scoping happens here in code
via Config.TARGET_FUNCTIONS — an unscoped query returns account-wide data
including other teams' workloads.
"""

from datetime import datetime, timezone

import boto3

from ..config import Config
from ...common.logging import get_logger, log_event
from .base import error_result, resolve_function, window_for

logger = get_logger("tool.metrics")
_cw = boto3.client("cloudwatch", region_name=Config.REGION)

# metric -> statistic. Counts are summed; duration is averaged.
LAMBDA_METRICS = {
    "Invocations": "Sum",
    "Errors": "Sum",
    "Throttles": "Sum",
    "Duration": "Average",
    "ConcurrentExecutions": "Maximum",
}
SQS_METRICS = {
    "ApproximateNumberOfMessagesVisible": "Maximum",
    "NumberOfMessagesSent": "Sum",
    "NumberOfMessagesDeleted": "Sum",
    "ApproximateAgeOfOldestMessage": "Maximum",
}

QUEUE_NAMES = {
    "queue": "sentry-capstone-orders-gulsher",
    "dlq": "sentry-capstone-orders-dlq-gulsher",
}


TOOL_SPEC = {
    "toolSpec": {
        "name": "get_metrics",
        "description": (
            "Get CloudWatch metrics around the incident for a target function or "
            "queue: invocations, errors, duration, throttles and queue depth. "
            "Use this to tell a genuine defect apart from a traffic increase: if "
            "errors rose in proportion to invocations, the code is behaving "
            "normally under more load. Also use it to check whether throttling or "
            "a capacity limit, rather than code, caused the failure. "
            "The result already contains `load_vs_defect`, which compares both "
            "the error rate and the average duration against traffic for you — "
            "read it rather than recomputing it. Note that a resource with zero "
            "errors can still be the failing one: check the duration fields "
            "before concluding the fault lies elsewhere. `series` holds "
            "per-bucket values for the metrics where timing matters; `summary` "
            "gives totals and trend for the rest. One call per resource is enough."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": ["api", "consumer", "queue", "dlq"],
                        "description": "Which resource's metrics to fetch.",
                    },
                    "period_seconds": {
                        "type": "integer",
                        "enum": [60, 300],
                        "description": "Datapoint granularity in seconds. Default 60.",
                    },
                },
                "required": ["target"],
            }
        },
    }
}


# Metrics whose SHAPE carries the diagnosis, so the raw series is kept.
#
# Everything else is summarised. The distinction is empirical: an earlier version
# summarised these two as well, and the agent compensated by making roughly twice
# as many tool calls — which cost more than the series ever did, because every
# turn resends all prior results. Under-informing is more expensive than
# over-informing here.
RAW_SERIES = {
    "ErrorRate",                        # load vs defect
    "Duration",                         # latency distribution and shape
    "ApproximateAgeOfOldestMessage",    # a queue backing up vs a spike
}


def _compact_series(points: list[dict], period: int) -> dict | None:
    """A dense encoding of an evenly spaced series.

    [{"timestamp": 1735689600, "value": 0.02}, ...] costs ~20 tokens per point,
    almost all of it repeated key names. Since the buckets are evenly spaced,
    a start plus a period plus a bare array says the same thing for ~3 tokens a
    point, with no information lost.
    """
    if not points:
        return None
    return {
        "start_timestamp": points[0]["timestamp"],
        "period_seconds": period,
        "values": [p["value"] for p in points],
    }


def _summarize(points: list[dict]) -> dict | None:
    if not points:
        return None
    values = [p["value"] for p in points]
    half = len(values) // 2 or 1
    return {
        "total": round(sum(values), 2),
        "max": round(max(values), 2),
        "mean": round(sum(values) / len(values), 3),
        "datapoints": len(values),
        "trend": ("rising" if sum(values[half:]) > sum(values[:half]) * 1.2
                  else "falling" if sum(values[half:]) < sum(values[:half]) * 0.8
                  else "flat"),
    }

def _fetch_series(namespace: str, metric: str, dimensions: list[dict],
                  start: int, end: int, period: int, stat: str) -> list[dict]:
    """One metric as an ordered list of {timestamp, value}."""
    resp = _cw.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=dimensions,
        StartTime=datetime.fromtimestamp(start, tz=timezone.utc),
        EndTime=datetime.fromtimestamp(end, tz=timezone.utc),
        Period=period,
        Statistics=[stat],
    )
    points = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
    return [
        {"timestamp": int(p["Timestamp"].timestamp()), "value": round(float(p[stat]), 3)}
        for p in points
    ]


def _error_rate(series: dict[str, list[dict]]) -> list[dict]:
    """Errors / Invocations per bucket.

    Computed here rather than asked of the model: arithmetic over a time series
    is exactly the kind of thing an LLM gets subtly wrong, and this ratio is the
    single most important signal for the load-vs-defect question.
    """
    invocations = {p["timestamp"]: p["value"] for p in series.get("Invocations", [])}
    errors = {p["timestamp"]: p["value"] for p in series.get("Errors", [])}
    rate = []
    for ts in sorted(invocations):
        total = invocations[ts]
        if total > 0:
            rate.append({
                "timestamp": ts,
                "value": round(errors.get(ts, 0.0) / total, 4),
            })
    return rate


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pct_change(before: float, after: float) -> float | None:
    if before == 0:
        return None                     # undefined, and not the same as 0%
    return round((after - before) / before * 100, 1)


def _duration_shift(series: dict[str, list[dict]]) -> dict | None:
    """How average duration moved across the window.

    The second axis of the same question. A latency alarm with a flat error
    rate is a real failure mode, and comparing errors against traffic alone
    reports it as "nothing happened here" — which sends the model looking at
    the wrong component.
    """
    points = series.get("Duration") or []
    if not points:
        return None

    values = [p["value"] for p in points]
    half = len(values) // 2 or 1
    before = _mean(values[:half])
    after = _mean(values[half:])
    return {
        "duration_before_ms": round(before, 1),
        "duration_after_ms": round(after, 1),
        "duration_change_pct": _pct_change(before, after),
    }


def _correlate(series: dict[str, list[dict]], rate: list[dict]) -> dict:
    """Did errors rise faster than traffic, or in step with it?

    Computed here for the same reason `_error_rate` is: this comparison is the
    entire purpose of the metrics tool, it is arithmetic over two series, and an
    LLM asked to do it either gets it subtly wrong or spends another tool call
    gathering more data to be sure.

    Deliberately reports the numbers and stops short of naming a root cause. The
    model still has to decide, and it still has to be free to answer 'unknown' —
    a verdict of 'errors outpaced load' is evidence for a defect, not proof of
    one, and phrasing it as a conclusion here would pressure the model into
    concluding.
    """
    invocations = series.get("Invocations") or []
    errors = series.get("Errors") or []
    latency = _duration_shift(series) or {}

    if not invocations:
        return {
            "verdict": "insufficient_data",
            **latency,
            "interpretation": (
                "No invocation datapoints in this window, so errors cannot be "
                "compared against traffic."
            ),
        }

    invocation_values = [p["value"] for p in invocations]
    total_invocations = sum(invocation_values)
    total_errors = sum(p["value"] for p in errors)

    half = len(invocation_values) // 2 or 1
    invocations_before = _mean(invocation_values[:half])
    invocations_after = _mean(invocation_values[half:])
    invocation_change = _pct_change(invocations_before, invocations_after)

    if total_errors == 0:
        # An alarm can fire on latency, throttles or queue depth, none of which
        # produce errors. Reporting only "no errors" here previously implied the
        # resource was healthy and pushed the model to look elsewhere, so the
        # duration axis has to be stated before that conclusion is available.
        duration_change = latency.get("duration_change_pct")
        if duration_change is not None and duration_change > 50:
            interpretation = (
                f"No errors were recorded, but average duration rose "
                f"{duration_change}% across the window, from "
                f"{latency['duration_before_ms']}ms to "
                f"{latency['duration_after_ms']}ms, while invocations changed by "
                f"{'an undefined amount' if invocation_change is None else f'{invocation_change}%'}. "
                f"The change here is in latency, not in error count."
            )
        else:
            interpretation = (
                "No errors were recorded for this resource in the window, and "
                "average duration did not change materially."
            )

        return {
            "verdict": "no_errors",
            "total_invocations": round(total_invocations, 2),
            "total_errors": 0.0,
            "invocations_change_pct": invocation_change,
            **latency,
            "interpretation": interpretation,
        }

    if not rate:
        return {
            "verdict": "insufficient_data",
            "total_invocations": round(total_invocations, 2),
            "total_errors": round(total_errors, 2),
            **latency,
            "interpretation": (
                "Errors occurred but no bucket had both traffic and error data, "
                "so the two cannot be compared."
            ),
        }

    rate_values = [p["value"] for p in rate]
    rate_half = len(rate_values) // 2 or 1
    rate_before = round(_mean(rate_values[:rate_half]), 4)
    rate_after = round(_mean(rate_values[rate_half:]), 4)

    # A rate that roughly holds while traffic moves is the signature of load; a
    # rate that climbs regardless of traffic is the signature of a defect. The
    # absolute floor stops rounding noise at tiny error counts from reading as a
    # meaningful jump.
    outpaced = rate_after > rate_before * 1.5 and (rate_after - rate_before) > 0.01

    if outpaced:
        verdict = "errors_outpaced_load"
        interpretation = (
            f"The error rate rose from {rate_before:.1%} to {rate_after:.1%} of "
            f"invocations while traffic changed by "
            f"{'an undefined amount' if invocation_change is None else f'{invocation_change}%'}. "
            f"Errors grew faster than traffic, so the failure is not explained by "
            f"volume alone."
        )
    else:
        verdict = "errors_tracked_load"
        interpretation = (
            f"The error rate held near {rate_before:.1%} to {rate_after:.1%} of "
            f"invocations while traffic changed by "
            f"{'an undefined amount' if invocation_change is None else f'{invocation_change}%'}. "
            f"Errors moved roughly in proportion to traffic rather than "
            f"independently of it."
        )

    return {
        "verdict": verdict,
        "total_invocations": round(total_invocations, 2),
        "total_errors": round(total_errors, 2),
        "error_rate_first_half": rate_before,
        "error_rate_second_half": rate_after,
        "invocations_change_pct": invocation_change,
        **latency,
        "interpretation": interpretation,
    }


def run(incident: dict, target: str = "api", period_seconds: int = 60) -> dict:
    try:
        start, end = window_for(incident)
    except Exception as exc:
        return error_result(str(exc))

    period = 60 if period_seconds not in (60, 300) else period_seconds

    try:
        if target in ("api", "consumer"):
            function_name = resolve_function(target)
            namespace = "AWS/Lambda"
            dimensions = [{"Name": "FunctionName", "Value": function_name}]
            wanted = LAMBDA_METRICS
            resource = function_name
        elif target in QUEUE_NAMES:
            namespace = "AWS/SQS"
            dimensions = [{"Name": "QueueName", "Value": QUEUE_NAMES[target]}]
            wanted = SQS_METRICS
            resource = QUEUE_NAMES[target]
        else:
            return error_result(f"unknown target {target!r}")
    except Exception as exc:
        return error_result(str(exc))

    series: dict[str, list[dict]] = {}
    for metric, stat in wanted.items():
        try:
            series[metric] = _fetch_series(
                namespace, metric, dimensions, start, end, period, stat
            )
        except Exception as exc:
            log_event(logger, "warning", f"metric {metric} failed: {exc}")
            series[metric] = []

    rate: list[dict] = []
    if namespace == "AWS/Lambda":
        rate = _error_rate(series)
        series["ErrorRate"] = rate

    total_points = sum(len(v) for v in series.values())
    log_event(logger, "info", "metrics fetched",
              target=target, resource=resource, points=total_points)

    payload = {
        "target": target,
        "resource": resource,
        "namespace": namespace,
        "window": {"start": start, "end": end, "period_seconds": period},
        "result_count": total_points,
        "has_data": total_points > 0,
        # Cheap, and enough for the metrics whose shape does not matter.
        "summary": {m: _summarize(pts) for m, pts in series.items() if pts},
        # Named explicitly so "no datapoints" reads as evidence rather than as a
        # metric the tool forgot to fetch.
        "no_data_for": sorted(m for m, pts in series.items() if not pts),
    }

    detail = {
        metric: _compact_series(series[metric], period)
        for metric in RAW_SERIES
        if series.get(metric)
    }
    if detail:
        payload["series"] = detail

    if namespace == "AWS/Lambda":
        payload["load_vs_defect"] = _correlate(series, rate)

    return payload