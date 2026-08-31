"""Structured JSON logging.

Every line the agent will later read comes out of here. Two rules:
  1. Always JSON — Logs Insights parses fields automatically, so the agent can
     filter on `level` and `correlation_id` instead of regexing free text.
  2. Always carry the correlation id — it is what lets the agent tie an error in
     the consumer back to the request that caused it in the API.
"""

import json
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")

SERVICE = os.environ.get("SERVICE_NAME", "unknown")
STAGE = os.environ.get("STAGE", "dev")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": SERVICE,
            "stage": STAGE,
            "correlation_id": _correlation_id.get(),
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
            payload["stack_trace"] = self.formatException(record.exc_info)

        # Anything passed via logger.info("msg", extra={"extra_fields": {...}})
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)

        return json.dumps(payload, default=str)


def get_logger(name: str = "app") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    return logger


def set_correlation_id(value: str | None = None) -> str:
    cid = value or str(uuid.uuid4())
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id.get()


def log_event(logger: logging.Logger, level: str, message: str, **fields: Any) -> None:
    """logger.info() with arbitrary structured fields attached."""
    getattr(logger, level.lower())(message, extra={"extra_fields": fields})
