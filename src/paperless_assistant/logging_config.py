"""Structured logging configuration for the service."""

from __future__ import annotations

import json
import logging
import logging.config
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_HTTP_URL_WITH_QUERY = re.compile(r"https?://[^\s\"']+\?[^\s\"']+", re.IGNORECASE)


def redact_url_query_values(message: str) -> str:
    """Redact every query-string value in absolute URLs embedded in a log message."""

    def redact_url(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlsplit(url)
        redacted_query = "&".join(
            f"{parameter.partition('=')[0]}=[REDACTED]" for parameter in parsed.query.split("&")
        )
        return urlunsplit(parsed._replace(query=redacted_query))

    return _HTTP_URL_WITH_QUERY.sub(redact_url, message)


class HttpRequestUrlRedactionFilter(logging.Filter):
    """Ensure third-party HTTP request logs cannot disclose query-string values."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_url_query_values(record.getMessage())
        record.args = ()
        return True


def install_http_request_url_redaction() -> None:
    """Install one process-wide, idempotent filter on HTTP client loggers."""
    for logger_name in ("httpx", "httpcore"):
        http_logger = logging.getLogger(logger_name)
        if not any(isinstance(item, HttpRequestUrlRedactionFilter) for item in http_logger.filters):
            http_logger.addFilter(HttpRequestUrlRedactionFilter())


class JsonFormatter(logging.Formatter):
    """Format application logs as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record without including exception-local secrets."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field in (
            "service",
            "version",
            "environment",
            "runtime",
            "ready",
            "operation",
            "status_code",
            "paperless_error",
            "truncated",
            "error_type",
        ):
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Configure root and server logging with a consistent JSON formatter."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["console"], "level": level},
        }
    )
    install_http_request_url_redaction()
