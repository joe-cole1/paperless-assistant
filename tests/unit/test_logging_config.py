"""Privacy-safe structured logging regression tests."""

from __future__ import annotations

import json
import logging
import sys

from paperless_assistant.logging_config import JsonFormatter


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
