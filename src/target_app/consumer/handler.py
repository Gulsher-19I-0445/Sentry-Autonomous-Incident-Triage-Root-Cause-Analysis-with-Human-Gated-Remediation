"""Consumer Lambda — processes orders off the queue.

This is where most chaos modes fire, and deliberately so: a failure here shows
up as DLQ depth or consumer errors, while the *cause* may have originated in the
API. That gap is exactly what makes root cause analysis non-trivial.

Partial batch failure reporting is enabled so one bad message does not fail the
whole batch — which also makes the retry_storm scenario behave realistically.
"""

import json
import os

from ..common import _internal
from ..common.logging import get_logger, log_event, set_correlation_id
from ..common.store import update_order_status

logger = get_logger("consumer")


def handler(event: dict, context) -> dict:
    failures: list[dict] = []

    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            _process(record, context)
        except Exception as exc:
            log_event(
                logger, "error", f"message processing failed: {exc}",
                error_type=type(exc).__name__,
                message_id=message_id,
                receive_count=record.get("attributes", {}).get("ApproximateReceiveCount"),
            )
            logger.exception("processing failed")
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}


def _process(record: dict, context) -> None:
    body = json.loads(record["body"])
    correlation_id = set_correlation_id(body.get("correlation_id"))

    log_event(logger, "info", "processing message",
              message_id=record["messageId"],
              request_id=context.aws_request_id)

    _internal._process_request("consumer")

    # KeyError here when the API enqueued a malformed payload (bad_payload mode).
    order_id = body["order_id"]

    gateway = os.environ.get("PAYMENT_GATEWAY_URL", "https://payments.internal/mock")
    log_event(logger, "debug", "calling payment gateway",
              order_id=order_id, gateway=gateway)

    update_order_status(order_id, "COMPLETED")

    log_event(logger, "info", "order completed",
              order_id=order_id, correlation_id=correlation_id)
