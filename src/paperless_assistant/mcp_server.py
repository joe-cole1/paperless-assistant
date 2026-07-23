"""MCP server construction."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from paperless_assistant.config import Settings
from paperless_assistant.tools.system import register_system_tools

MCP_INSTRUCTIONS = """Paperless Assistant is currently a bootstrap connectivity probe.
Only the harmless ping tool is available. This server has no Paperless document access,
credentials, upload capability, metadata write capability, Gemini API integration, or
Discord integration. Do not treat it as a document system until authentication and
read-only Paperless access are implemented in later reviewed phases.
"""


def create_mcp_server(settings: Settings) -> FastMCP[None]:
    """Build a stateless Streamable HTTP MCP server with DNS-rebinding protection."""
    mcp: FastMCP[None] = FastMCP(
        name=settings.app_name,
        instructions=MCP_INSTRUCTIONS,
        log_level=settings.log_level,
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.mcp_allowed_hosts),
            allowed_origins=list(settings.mcp_allowed_origins),
        ),
    )
    register_system_tools(mcp, settings)
    return mcp
