"""Structured logging configuration with secret redaction.

Logs never record session cookies, authorization headers, or API keys. Use
:func:`redact_headers` / :func:`redact_mapping` before logging any header or
cookie mapping, and attach a per-request id via :data:`request_id_var` so log
lines can be correlated.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

#: Correlation id for the in-flight request, surfaced in every log record.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

#: Header/field names whose values must never be logged.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "apikey",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "secret",
    }
)

_REDACTED = "***REDACTED***"

# Attributes that exist on every LogRecord; anything else is treated as "extra"
# and included in the structured output.
_STANDARD_RECORD_ATTRS = frozenset(
    logging.makeLogRecord({}).__dict__.keys()
    | {"message", "asctime", "taskName"}
)


def redact_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``mapping`` with sensitive values replaced."""

    redacted: dict[str, Any] = {}
    for key, value in mapping.items():
        if key.lower() in SENSITIVE_KEYS:
            redacted[key] = _REDACTED
        else:
            redacted[key] = value
    return redacted


def redact_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    """Alias of :func:`redact_mapping` with header semantics."""

    return redact_mapping(headers)


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # Include any structured "extra" attributes supplied at the call site.
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value

        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-friendly formatter that still surfaces the request id."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = request_id_var.get()
        return super().format(record)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure the root logger. Idempotent across repeated calls."""

    root = logging.getLogger()
    root.setLevel(level.upper())

    # Replace existing handlers so re-configuration (e.g. in tests) is clean.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())
    root.addHandler(handler)

    # Tame noisy third-party loggers.
    for noisy in ("urllib3", "asyncio", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger."""

    return logging.getLogger(name)
