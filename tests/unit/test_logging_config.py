"""Privacy-safe structured logging regression tests."""

from __future__ import annotations

import json
import logging
import sys

from paperless_assistant.logging_config import (
    HttpRequestUrlRedactionFilter,
    JsonFormatter,
    redact_url_query_values,
)


def test_json_formatter_reports_exception_type_without_exception_text() -> None:
    try:
        raise RuntimeError("private synthetic detail")
    except RuntimeError:
        record = logging.LogRecord(
            "paperless-assistant",
            logging.ERROR,
            __file__,
            1,
            "safe_event",
            (),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["exception_type"] == "RuntimeError"
    assert payload["event"] == "safe_event"
    assert "private synthetic detail" not in json.dumps(payload)


def test_http_url_redaction_preserves_parameter_names() -> None:
    message = (
        "HTTP Request: GET http://paperless.test/api/documents/?text=private-text"
        '&title_search=private-title&query=private-query&page=1 "HTTP/1.1 200 OK"'
    )

    redacted = redact_url_query_values(message)

    assert "private-text" not in redacted
    assert "private-title" not in redacted
    assert "private-query" not in redacted
    assert "text=[REDACTED]" in redacted
    assert "title_search=[REDACTED]" in redacted
    assert "query=[REDACTED]" in redacted
    assert "page=[REDACTED]" in redacted


def test_http_url_redaction_filter_rewrites_formatted_arguments() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: %s",
        ("https://paperless.test/api/documents/?text=private-marker",),
        exc_info=None,
    )

    accepted = HttpRequestUrlRedactionFilter().filter(record)

    assert accepted is True
    assert record.args == ()
    assert "private-marker" not in record.getMessage()
    assert "text=[REDACTED]" in record.getMessage()
