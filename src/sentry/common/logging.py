"""Structured JSON logging for the Sentry pipeline.

Deliberately separate from target_app's logger: these logs are Sentry's own
telemetry, and must NOT sit under the log group prefix the agent queries. The
agent reading its own reasoning as evidence would be a nasty feedback loop.
"""

import json
import logging
import os
import sys
import time
from typing import Any

SERVICE = os.environ.get("SERVICE_NAME", "sentry")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": SERVICE,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
            payload["stack_trace"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, default=str)


def get_logger(name: str = "sentry") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    return logger


def log_event(logger: logging.Logger, level: str, message: str, **fields: Any) -> None:
    getattr(logger, level.lower())(message, extra={"extra_fields": fields})