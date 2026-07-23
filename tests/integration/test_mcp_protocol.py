"""End-to-end MCP Streamable HTTP protocol test using the official client."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from paperless_assistant.app import create_app
from paperless_assistant.config import Settings


@asynccontextmanager
async def running_server() -> AsyncIterator[str]:
    """Start Uvicorn on an ephemeral loopback port and stop it cleanly."""
    application = create_app(Settings(_env_file=None))
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=0, log_config=None, access_log=False)
    )
    listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen_socket.bind(("127.0.0.1", 0))
    listen_socket.listen()
    listen_socket.setblocking(False)
    port = listen_socket.getsockname()[1]
    task = asyncio.create_task(server.serve(sockets=[listen_socket]))

    for _ in range(100):
        if server.started:
            break
        if task.done():
            await task
        await asyncio.sleep(0.01)
    else:
        server.should_exit = True
        await task
        raise RuntimeError("Uvicorn did not start")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_official_client_lists_and_calls_only_ping() -> None:
    async with (
        running_server() as url,
        streamable_http_client(url) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialize_result = await session.initialize()
        tools_result = await session.list_tools()
        call_result = await session.call_tool("ping")

    assert initialize_result.serverInfo.name == "paperless-assistant"
    assert initialize_result.instructions is not None
    assert "no Paperless document access" in initialize_result.instructions
    assert [tool.name for tool in tools_result.tools] == ["ping"]
    assert call_result.isError is False
    assert call_result.structuredContent == {
        "status": "ok",
        "message": "Paperless MCP connection successful",
        "service": "paperless-assistant",
        "version": "0.1.0",
        "bootstrap_mode": True,
    }
