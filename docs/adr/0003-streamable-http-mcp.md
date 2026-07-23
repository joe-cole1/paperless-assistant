# ADR 0003: Stateless Streamable HTTP for MCP

- Status: Accepted
- Date: 2026-07-23

## Context

Gemini Spark custom connected apps accept a remote MCP endpoint. The MCP specification defines Streamable HTTP as the production HTTP transport. The bootstrap needs a reverse-proxy-friendly endpoint with no session persistence requirement.

## Decision

Use the stable v1 line of the official MCP Python SDK and FastMCP with Streamable HTTP at exactly `/mcp`, stateless mode, and JSON responses. Enable the SDK's DNS-rebinding protection with explicit Host and Origin allowlists. The ASGI lifecycle starts and stops the MCP session manager cleanly.

## Consequences

The endpoint works behind Pangolin without sticky sessions and is exercised by the official client in integration tests. Production host/origin values must be configured explicitly. Authentication remains mandatory before any document capability is exposed.

## Alternatives considered

- Legacy HTTP+SSE: superseded for new remote deployments.
- Stateful Streamable HTTP: supports resumable sessions but adds state that the one-tool probe does not need.
- A custom protocol implementation: unnecessary risk compared with the official SDK.
