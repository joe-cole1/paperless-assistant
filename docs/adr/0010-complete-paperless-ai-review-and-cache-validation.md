# ADR 0010: Complete Paperless AI review and validate its cache as an external contract

Status: Accepted

Date: 2026-07-28

Issues: #44, #70

Supersedes: ADR 0009's decision that unmatched taxonomy names are display-only

## Context

Paperless-ngx 3.0.4 returns two kinds of metadata from
`GET /api/documents/{id}/ai_suggestions/`: IDs for names it matched to visible existing taxonomy
objects and strings for names it could not match. The assistant parsed both, but its Discord card
flattened unmatched correspondents, document types, storage paths, and tags into one
“new taxonomy names” paragraph. Users could not select individual candidates, compare close
existing objects, or confirm creation.

Production diagnostics against fresh and previously processed documents proved that this endpoint
invokes the configured LLM on a cache miss and returns the cached LLM response thereafter. Opening
the Paperless page or pressing its UI button is not required. Paperless invalidates that cache when
the document changes. Paperless exposes no supported force-regeneration API in 3.0.4.

## Decision

The assistant loads one user-scoped review containing the document concurrency baseline, the full
AI response, every visible taxonomy object, and the invoking user's taxonomy-creation permissions.
Discord presents matched existing objects and unmatched AI names in the same category selector:

- matched existing IDs are selected by default;
- unmatched names are unselected by default;
- close visible existing objects are offered before creation;
- correspondent, document type, and storage path are single-select;
- tags are multi-select and Discord's 25-option bound is handled with private pagination;
- current and selected values are shown before apply.

Applying still requires the uploader's explicit action and a fresh `modified` comparison.
Unspecified scalar fields and existing tags are preserved. Tags are added through Paperless's
`modify_tags` bulk-edit operation rather than replacing the document tag list.

An unmatched selected name requires a second confirmation. Immediately before creation, the
application performs an exact case-insensitive lookup, uses one exact match if it now exists,
fails closed on ambiguity, rechecks the user's `add_*` permission, and only then creates the
object with automatic matching disabled. Storage-path names use the same selected value as their
initial path. Exact lookup makes a safe retry converge after a partially completed creation
sequence. Audit records contain only principal/object kind and durable identifiers, never the
suggested name.

The Discord control is named **Reload Review**, not **Regenerate**. It performs the same supported
GET and explicitly warns that Paperless may return its cached response. The assistant does not
mutate documents, add temporary tags, access Paperless internals, or otherwise attempt to evict
the cache.

A disposable integration stack pins Paperless-ngx 3.0.4 by immutable digest and uses a local
deterministic OpenAI-compatible server plus a synthetic PDF. It proves LLM request construction,
matched IDs, unmatched names, dates, cache hit, document-change invalidation, and metadata
writeback. The fixture never logs the document prompt, authorization header, or synthetic API key.

## Consequences

- Users can customize the complete Paperless AI proposal without leaving Discord.
- Taxonomy expansion is possible only with separate confirmation and the linked user's Paperless
  permissions.
- A reload is honest about cache semantics and does not promise a new paid or local LLM call.
- AI-disabled, invalid-configuration, timeout, permission, and transport failures remain generic
  in Discord but have distinct exception types and privacy-safe operator diagnostics.
- The full ship gate includes a heavier Docker integration test in addition to unit and container
  smoke tests.

## Alternatives considered

- Keep unmatched names display-only: rejected because issue #44 explicitly requires a confirmed
  create-and-apply path.
- Add and remove a temporary “cache” tag: rejected because it mutates user data to exploit an
  implementation detail and can produce partial writes or audit noise.
- Call the classic `/suggestions/` endpoint as a fallback: rejected because those are not the
  configured LLM's results.
- Label reload as regenerate: rejected because Paperless 3.0.4 may correctly return a cache hit.
