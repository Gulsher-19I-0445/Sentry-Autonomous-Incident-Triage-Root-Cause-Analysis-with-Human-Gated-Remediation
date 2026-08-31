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
            "queue: invocations, errors, duration, throttles and queue depth, as a "
            "time series. Use this to tell a genuine defect apart from a traffic "
            "increase: if errors rose in proportion to invocations, the code is "
            "behaving normally under more load. Also use it to check whether "
            "throttling or a capacity limit, rather than code, caused the failure."
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

    if namespace == "AWS/Lambda":
        series["ErrorRate"] = _error_rate(series)

    total_points = sum(len(v) for v in series.values())
    log_event(logger, "info", "metrics fetched",
              target=target, resource=resource, points=total_points)

    return {
        "target": target,
        "resource": resource,
        "namespace": namespace,
        "window": {"start": start, "end": end, "period_seconds": period},
        "result_count": total_points,
        "has_data": total_points > 0,
        "summary": {m: _summarize(pts) for m, pts in series.items() if pts},
    }