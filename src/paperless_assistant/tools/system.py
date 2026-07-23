"""Harmless bootstrap system tools."""

from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict

from paperless_assistant.config import Settings


class PingResult(BaseModel):
    """Structured result returned by the bootstrap ``ping`` tool."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"
    message: str = "Paperless MCP connection successful"
    service: str
    version: str
    bootstrap_mode: bool


def ping(settings: Settings) -> PingResult:
    """Return non-sensitive service connectivity metadata."""
    return PingResult(
        service=settings.app_name,
        version=settings.app_version,
        bootstrap_mode=settings.mcp_bootstrap_mode,
    )


def register_system_tools(mcp: FastMCP[None], settings: Settings) -> None:
    """Register only the tools permitted during bootstrap."""

    async def ping_tool() -> PingResult:
        """Confirm MCP connectivity without accessing documents or credentials."""
        return ping(settings)

    mcp.add_tool(
        ping_tool,
        name="ping",
        description=(
            "Confirm connectivity to the Paperless Assistant bootstrap MCP server. "
            "This tool has no document access and causes no side effects."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
        structured_output=True,
    )
