"""Chaos injection.

Design choice worth understanding: chaos is ARMED, not fired directly. You arm
a mode, then ordinary traffic triggers it. That matters because the incident
must look like a real production failure — an alarm firing during normal use —
not like someone hitting a /break endpoint. The agent should never be able to
tell the difference.

Each mode maps to a scenario in the evaluation set.
"""

import os
import time
from typing import NoReturn

from .logging import get_logger, log_event
from .store import consume_flag, get_flag

logger = get_logger("chaos")

# mode -> (where it fires, scenario id, what the true root cause is)
MODES: dict[str, dict[str, str]] = {
    "exception": {
        "component": "api",
        "scenario": "S01",
        "true_cause": "code_defect",
        "description": "Unhandled KeyError on a missing field",
    },
    "slow": {
        "component": "api",
        "scenario": "S04",
        "true_cause": "code_defect",
        "description": "Slow dependency call breaching latency p95",
    },
    "memory": {
        "component": "consumer",
        "scenario": "S07",
        "true_cause": "code_defect",
        "description": "Unbounded list allocation exhausting memory",
    },
    "timeout": {
        "component": "consumer",
        "scenario": "S04b",
        "true_cause": "code_defect",
        "description": "Handler exceeds its configured timeout",
    },
    "denied": {
        "component": "consumer",
        "scenario": "S02",
        "true_cause": "config",
        "description": "IAM AccessDenied after a permissions change",
    },
    "bad_payload": {
        "component": "api",
        "scenario": "S05",
        "true_cause": "code_defect",
        "description": "API enqueues a malformed message the consumer cannot parse",
    },
    "missing_env": {
        "component": "consumer",
        "scenario": "S06",
        "true_cause": "config",
        "description": "Required environment variable absent after a config change",
    },
    "retry_storm": {
        "component": "consumer",
        "scenario": "S09",
        "true_cause": "code_defect",
        "description": "Handler always raises, so messages retry until the DLQ",
    },
    "silent": {
        "component": "consumer",
        "scenario": "S14",
        "true_cause": "unknown",
        "description": "Failure with the error swallowed — minimal evidence, agent should escalate",
    },
}


def is_armed(mode: str, component: str) -> bool:
    if mode not in MODES:
        return False
    if MODES[mode]["component"] != component:
        return False
    flag = get_flag(f"chaos_{mode}")
    return bool(flag and flag.get("enabled"))


def _apply(mode: str) -> NoReturn | None:
    """Execute the armed chaos mode. Most of these raise."""
    consume_flag(f"chaos_{mode}")

    # Deliberately vague log line. A real incident does not announce its cause,
    # and the agent has to work it out from evidence rather than be told.
    log_event(logger, "debug", "processing request", stage="pre_handler")

    if mode == "exception":
        payload: dict = {}
        _ = payload["customer"]["tier"]  # KeyError

    elif mode == "slow":
        time.sleep(4.5)

    elif mode == "memory":
        hog = []
        while True:
            hog.append("x" * 1_000_000)

    elif mode == "timeout":
        time.sleep(300)

    elif mode == "denied":
        import boto3
        boto3.client("s3").get_object(
            Bucket=os.environ.get("FORBIDDEN_BUCKET", "sentry-no-such-bucket-xyz"),
            Key="config.json",
        )

    elif mode == "missing_env":
        _ = os.environ["PAYMENT_GATEWAY_URL"]  # KeyError

    elif mode == "retry_storm":
        raise RuntimeError("downstream call failed")

    elif mode == "silent":
        try:
            raise ValueError("internal state inconsistent")
        except ValueError:
            log_event(logger, "warning", "recoverable condition, continuing")
            raise RuntimeError("processing failed")

    return None


def _process_request(component: str) -> None:
    """Call at the top of a handler. Fires whichever mode is armed for it."""
    for mode in MODES:
        if is_armed(mode, component):
            log_event(logger, "debug", "entering handler", component=component)
            _apply(mode)
