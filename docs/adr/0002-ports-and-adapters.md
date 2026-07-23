# ADR 0002: Ports-and-adapters modular monolith

- Status: Accepted
- Date: 2026-07-23

## Context

MCP, Discord, Paperless, LLMs, audit storage, and file delivery will evolve at different rates. Premature microservices would add deployment and consistency cost, while putting business logic in transport handlers would tightly couple those systems.

## Decision

Use a modular monolith with domain models and application services behind explicit ports. MCP and Discord are inbound adapters. Paperless, LLM, audit, and delivery implementations are outbound adapters. Both front ends call shared application services; neither calls the other adapter. Deploy Discord as a separate worker process from the same package when implemented.

Phase 0 implements only the configuration, HTTP/MCP inbound adapter, and harmless system tool required for connectivity.

## Consequences

Core policies remain testable without transports, and adapters can be replaced independently. Boundaries require discipline even though code ships from one repository. Unused interface scaffolding is deferred until a use case needs it.

## Alternatives considered

- Transport-centric handlers: initially simple but duplicates policy and couples integrations.
- Microservices: independently deployable but operationally excessive for the current household-scale system.
- A shared library plus separate repositories: creates version coordination before it provides value.
