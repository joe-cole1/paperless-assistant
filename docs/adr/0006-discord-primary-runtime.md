# ADR 0006: Discord is the primary runtime

- Status: Accepted
- Date: 2026-07-25
- Supersedes: ADR 0003 and the separate-second-worker portion of ADR 0005

## Context

Paperless Assistant began as a public MCP connectivity probe intended for Gemini Spark. That
direction required public ingress, an OAuth resource-server boundary, JWT/JWKS validation,
Pangolin routing, and an HTTP transport that did not serve the household's primary workflow.
The product goal is now a private Discord assistant for native Paperless-ngx chat, document
delivery, and ingestion.

The issue #6 OAuth implementation and its draft ADR were preserved only in a local WIP commit.
They were never merged or accepted on the default branch.

## Decision

Discord is the only user interface and the primary application runtime. It connects outbound
through the Discord Gateway and calls shared application services inside the modular monolith.
Paperless-ngx remains an outbound adapter.

Remove MCP, OAuth/JWT/JWKS, Gemini Spark, and Pangolin runtime code, dependencies, configuration,
tests, documentation, and public routes. Keep a small private ASGI surface containing only
non-secret service metadata, `/healthz`, and `/readyz`. Compose may bind that surface to NAS
loopback for monitoring, but it is not an internet-facing application API.

The hardened container remains non-root with a read-only root filesystem, dropped Linux
capabilities, `no-new-privileges`, and bounded temporary storage. The Discord feature issue will
add only the restricted writable state required for SQLite and staged files.

## Consequences

The service no longer carries an unused public authentication or protocol stack. Discord and
Paperless availability can be represented separately from process liveness. A future inbound
interface requires its own issue, authentication design, ADR, and security review.

Discord is no longer a second worker beside MCP; it becomes the single primary worker. The
ports-and-adapters decision in ADR 0002 remains accepted.

## Alternatives considered

- Keep the MCP probe disabled: rejected because dormant dependencies, configuration, and routes
  still create maintenance and attack surface.
- Run Discord beside the MCP server: rejected because MCP no longer serves a product use case.
- Use Discord HTTP interactions: rejected for the initial deployment because it would require a
  public inbound endpoint; the Gateway provides the required outbound connection model.
