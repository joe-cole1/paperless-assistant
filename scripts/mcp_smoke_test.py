#!/usr/bin/env python3
"""Call the running bootstrap service through the official MCP client."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

EXPECTED_RESULT: dict[str, Any] = {
    "status": "ok",
    "message": "Paperless MCP connection successful",
    "service": "paperless-assistant",
    "version": "0.1.0",
    "bootstrap_mode": True,
}


async def run(url: str) -> None:
    """Initialize a session, verify the tool catalog, and call ``ping``."""
    async with (
        streamable_http_client(url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = [tool.name for tool in tools.tools]
        if names != ["ping"]:
            raise RuntimeError(f"expected only ping, received: {names}")
        result = await session.call_tool("ping")
        if result.isError or result.structuredContent != EXPECTED_RESULT:
            raise RuntimeError(f"unexpected ping result: {result}")
    sys.stdout.write("MCP ping succeeded with the expected structured response.\n")


def main() -> None:
    """Parse command-line arguments and execute the asynchronous smoke test."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8780/mcp")
    arguments = parser.parse_args()
    asyncio.run(run(arguments.url))


if __name__ == "__main__":
    main()
