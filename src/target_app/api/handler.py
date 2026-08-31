"""API Lambda — front door of the target app.

Deployed behind a Lambda Function URL (no API Gateway needed for the target app)
and published behind an ALIAS. The alias matters: rolling the alias back to the
previous version is one of the two remediation actions the executor can take.

Routes:
    POST /orders              create an order, enqueue for processing
    GET  /orders/{id}         read an order
    POST /admin/chaos/{mode}  arm a chaos mode
    GET  /admin/flags         list armed flags
    DELETE /admin/chaos/{mode} disarm
"""

import json
import os
import uuid

import boto3

from ..common import _internal
from ..common.logging import get_logger, log_event, set_correlation_id, get_correlation_id
from ..common.store import get_order, list_flags, put_order, set_flag, clear_flag

logger = get_logger("api")
sqs = boto3.client("sqs")

QUEUE_URL = os.environ["QUEUE_URL"]
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _route(event: dict) -> tuple[str, str]:
    ctx = event.get("requestContext", {}).get("http", {})
    return ctx.get("method", "GET"), event.get("rawPath", "/")


def handler(event: dict, context) -> dict:
    correlation_id = set_correlation_id(
        event.get("headers", {}).get("x-correlation-id")
    )
    method, path = _route(event)

    log_event(logger, "info", "request received",
              method=method, path=path, request_id=context.aws_request_id)

    try:
        # Admin routes bypass chaos so you can always disarm.
        if path.startswith("/admin/"):
            return _handle_admin(method, path, event)

        # Armed chaos fires here, during ordinary traffic.
        _internal._process_request("api")

        if method == "POST" and path == "/orders":
            return _create_order(event)
        if method == "GET" and path.startswith("/orders/"):
            return _get_order(path.rsplit("/", 1)[-1])

        return _response(404, {"error": "not found", "correlation_id": correlation_id})

    except Exception as exc:
        # Logged with a stack trace, then re-raised so the Lambda Errors metric
        # increments and the alarm fires. Both halves matter.
        log_event(logger, "error", f"unhandled error: {exc}",
                  error_type=type(exc).__name__, path=path)
        logger.exception("request failed")
        raise


def _create_order(event: dict) -> dict:
    body = json.loads(event.get("body") or "{}")
    order_id = str(uuid.uuid4())[:8]

    put_order(order_id, {
        "item": body.get("item", "widget"),
        "quantity": int(body.get("quantity", 1)),
    })

    message = {"order_id": order_id, "correlation_id": get_correlation_id()}

    # bad_payload chaos corrupts the message so the CONSUMER fails, not the API.
    # Good scenario: the alarm fires on a component that is not the faulty one.
    if _internal.is_armed("bad_payload", "api"):
        message = {"orderId": order_id, "qty": "not-a-number"}
        log_event(logger, "info", "order enqueued", order_id=order_id)

    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(message))

    log_event(logger, "info", "order created", order_id=order_id)
    return _response(201, {"order_id": order_id, "status": "PENDING"})


def _get_order(order_id: str) -> dict:
    order = get_order(order_id)
    if not order:
        return _response(404, {"error": "order not found", "order_id": order_id})
    return _response(200, order)


def _handle_admin(method: str, path: str, event: dict) -> dict:
    token = event.get("headers", {}).get("x-admin-token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        return _response(403, {"error": "forbidden"})

    if method == "GET" and path == "/admin/flags":
        return _response(200, {"flags": list_flags()})

    if path.startswith("/admin/chaos/"):
        mode = path.rsplit("/", 1)[-1]
        if mode not in _internal.MODES:
            return _response(400, {"error": "unknown mode",
                                   "available": list(_internal.MODES)})
        if method == "DELETE":
            clear_flag(f"chaos_{mode}")
            return _response(200, {"disarmed": mode})

        body = json.loads(event.get("body") or "{}")
        flag = set_flag(
            f"chaos_{mode}",
            enabled=True,
            ttl_seconds=int(body.get("ttl_seconds", 900)),
            remaining=int(body.get("remaining", 5)),
            scenario=_internal.MODES[mode]["scenario"],
        )
        # Armed state is logged for YOUR audit trail, never for the agent:
        # keep this out of the agent's queryable log groups.
        log_event(logger, "info", "chaos armed", mode=mode,
                  scenario=_internal.MODES[mode]["scenario"])
        return _response(200, {"armed": mode, "flag": flag})

    return _response(404, {"error": "not found"})
