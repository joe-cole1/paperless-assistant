# ADR 0009: Harden AI metadata review, diagnostics, cleanup, credentials, and CI writeback

Status: Accepted

Date: 2026-07-28

Issue: #65

## Context

The upload workflow needs Paperless-ngx 3.0.4's LLM suggestions, not the separate classic
classifier suggestions. The former implementation called both endpoints concurrently, used a
client timeout shorter than Paperless's default LLM timeout, silently fell back to classifier
results, and parsed the AI title with the wrong shape. Metadata controls were available to any
allowlisted user and could erase tags or overwrite a newer Paperless edit.

The same review found fail-open authorization when the user allowlist was empty, a known default
credential-encryption secret with a fast passphrase derivation, destructive cleanup on ambiguous
Paperless failures, incomplete Discord cleanup identities, raw upstream failures crossing into
Discord, and a formatting workflow that exposed a write token while processing PR code.

## Decision

The assistant calls only:

```text
GET /api/documents/{id}/ai_suggestions/
```

for post-upload AI review. In Paperless 3.0.4 this GET synchronously triggers the configured LLM
when no cached result exists. The response is parsed as title, matched IDs, unmatched names, and
dates. The assistant timeout defaults to 150 seconds and must remain above Paperless's default
`PAPERLESS_AI_LLM_REQUEST_TIMEOUT=120`. Embeddings remain optional; Paperless may use its LLM index
for similar-document context when configured.

Only the durable job's uploader may edit, reload, apply, or cancel the suggestion view. Apply is
serialized, reloads the document, rejects a changed `modified` value, preserves all existing tags,
and PATCHes only confirmed title, correspondent, document type, storage path, created date, and
matched tag IDs. Unmatched names are displayed but are not created.

Paperless errors shown in Discord remain generic. The Paperless adapter logs a constant event with
normalized operation, status, a 4 KiB maximum diagnostic, truncation state, control-character
escaping, and credential-shaped value redaction. Authorization headers are never logged.

Configuration requires a non-empty immutable Discord user allowlist and a generated URL-safe
base64 Fernet key. A revoked user credential never falls back to the system token; durable staged
or submitted jobs fail closed and their staged bytes are removed.

Inbox cleanup obtains document tags in one bounded batch request. Any taxonomy or document-list
failure produces no deletion candidates. Cleanup stores exact channel/message targets and retains
job or context evidence until Discord confirms each target was deleted or already absent.

Pull-request CI performs Ruff fixes and formatting in a credential-free job. Quality and container
checks run against that patch. A separate same-repository-only job receives `contents: write`
after all validation, accepts only a bounded Python patch artifact, disables hooks, and pushes a
formatting-only commit. Fork PRs remain read-only.

## Consequences

- Operators must generate a new Fernet key. Credentials encrypted with the old passphrase-derived
  scheme are intentionally unreadable and users must relink.
- A Paperless AI outage is visible instead of being disguised as classic suggestions.
- Metadata updates can be rejected as stale; the uploader reloads and reviews a fresh proposal.
- CI may add one formatting commit to an in-repository PR after checks pass. Fork contributors
  must format locally.
- Server logs are more diagnostically useful but remain sensitive operator data and require
  restricted retention and access.

## Alternatives considered

- Continue racing AI and classic suggestions: rejected because it hides the health and output of
  the requested LLM feature.
- Automatically create unmatched taxonomy objects: rejected because an LLM must not expand the
  Paperless taxonomy without a separate confirmed workflow.
- Apply against the cached document without merging tags: rejected because it can overwrite
  concurrent edits and erase existing classification.
- Give the validation job a write token: rejected because PR-controlled code and write authority
  must remain separated.
