"""Deterministic OpenAI-compatible fixture for Paperless integration tests."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_lock = threading.Lock()


@dataclass
class State:
    request_count: int = 0
    saw_synthetic_marker: bool = False
    saw_tool_definition: bool = False


_state = State()

_SUGGESTIONS = {
    "title": "Synthetic Chat Conversation with Mary",
    "tags": ["conversation", "new-topic"],
    "correspondents": ["Mary", "Abby"],
    "document_types": ["Chat Log", "Text Message"],
    "storage_paths": ["Personal/Chat Logs", "Messages/Mary"],
    "dates": ["2026-07-28"],
}


class Handler(BaseHTTPRequestHandler):
    """Serve only the fixture endpoints needed by Paperless's OpenAI client."""

    server_version = "synthetic-openai"

    def log_message(self, _format: str, *args: Any) -> None:
        del _format, args

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/stats":
            self._json(404, {"error": "not_found"})
            return
        with _lock:
            payload = {
                "request_count": _state.request_count,
                "saw_synthetic_marker": _state.saw_synthetic_marker,
                "saw_tool_definition": _state.saw_tool_definition,
            }
        self._json(200, payload)

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not_found"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(content_length))
            messages = payload["messages"]
            tools = payload["tools"]
            tool_name = tools[0]["function"]["name"]
        except IndexError, KeyError, TypeError, ValueError:
            self._json(400, {"error": "malformed_request"})
            return
        marker_seen = "SYNTHETIC PAPERLESS AI CACHE TEST" in json.dumps(messages)
        with _lock:
            _state.request_count += 1
            _state.saw_synthetic_marker = _state.saw_synthetic_marker or marker_seen
            _state.saw_tool_definition = True
        self._json(
            200,
            {
                "id": "synthetic-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "synthetic-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "synthetic-tool-call",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(_SUGGESTIONS),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
