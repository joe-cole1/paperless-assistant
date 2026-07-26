# ADR 0005: Discord as a separate worker using the shared core

- Status: Superseded in part by ADR 0006
- Date: 2026-07-23

## Context

The MCP HTTP server and a Discord Gateway or interaction client have different lifecycle, connectivity, scaling, and exposure needs. They must nevertheless apply the same retrieval, authorization, audit, and delivery rules.

## Decision

When implemented, run `paperless-discord` as a separate process or container built from this repository. It imports shared application services and ports. It does not call MCP tools or automate the Gemini Spark UI. Question answering uses a supported provider through an `LLMProvider` port.

## Consequences

The public MCP process and outbound Discord worker can restart and scale independently while sharing policy code. Deployment has a second runtime to operate. Durable jobs or audit storage are added only when workflow reliability requires them.

## Alternatives considered

- Run Discord in the MCP process: fewer containers but coupled failures and lifecycle complexity.
- Have Discord call the public MCP endpoint: duplicates authentication and makes one inbound adapter depend on another.
- Separate Discord repository: adds release coordination and risks policy drift.
