# ADR 0004: Read-only-first capability model

- Status: Accepted
- Date: 2026-07-23

## Context

Paperless contains private family documents. Natural-language interfaces and externally reachable endpoints expand the impact of authentication mistakes, prompt injection, excessive retrieval, and unintended mutations.

## Decision

Capabilities are introduced in phases and fail closed. Phase 0 has no Paperless credential or access. The first integration uses a dedicated read-only account, bounded retrieval, explicit scopes, and authenticated MCP. Downloads, ingestion, and writes are separate capabilities. Writes require separate credentials and scopes, explicit confirmation, idempotency, dry runs, bounded targets, audit records, and rollback planning.

## Consequences

Useful functionality arrives more slowly, but security reviews have small, understandable surfaces. A hard-to-guess URL is never authorization. Destructive operations remain prohibited unless separately approved.

## Alternatives considered

- One privileged Paperless token: simpler configuration but violates least privilege and increases blast radius.
- Enable all tools behind proxy login: Pangolin's interactive login is not an MCP authorization protocol.
- Natural-language bulk writes: rejected because targets and consequences are insufficiently bounded.
