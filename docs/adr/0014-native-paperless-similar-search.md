# ADR 0014: Use owner-only native Paperless similarity from document results

Status: Accepted

Date: 2026-07-29

Issue: #43

## Context

Document result cards offered a Dismiss action that deleted one Discord message but did not help
users continue retrieval. Paperless already provides permission-filtered native similarity through
`GET /api/documents/?more_like_id={document_id}`. Adding a second similarity model or index would
duplicate Paperless behavior and expand the private-data boundary.

Result controls remain visible after bot restarts, so the assistant must reject forged, stale, and
cross-user component identifiers without placing queries or credentials in Discord component
state.

## Decision

Replace Dismiss on document result cards with Similar. The component identifier contains only the
original requester and source document IDs. Before searching, the Discord adapter verifies the
guild, questions channel or thread, exact requester, allowlist membership, and unexpired
thread-scoped reference context.

The application service loads the requester's encrypted Paperless token and calls the gateway's
bounded `more_like_id` operation. Paperless remains responsible for object-permission filtering.
The gateway requests four candidates, removes the source document defensively, and returns at most
three. Results render in the existing public query thread and replace its short-lived result
context. Audit records contain only the principal, operation, outcome, timestamp, and correlation
ID.

Legacy upload-status notifications retain a separately named Dismiss control. Old
`paperless:dismiss:*` result controls receive a bounded expiration response after deployment.

## Consequences

- Similar retrieval uses no additional model, index, credential, or public route.
- Only the original requester can invoke a result card, and Paperless limits that user's visible
  documents.
- Results remain intentionally bounded to the existing three-card Discord experience.
- Stale or deleted sources, permission failures, and empty matches fail safely without exposing
  Paperless response bodies.
- Broader selectable search modes and pagination remain outside issue #43.
