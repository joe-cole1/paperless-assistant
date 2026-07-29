# ADR 0015: Finalize AI review tags only after a confirmed save

Status: Accepted

Date: 2026-07-29

Issue: #83

## Context

Paperless inbox tags identify documents awaiting review, while Discord holds the uploader's
durable per-file AI review. Previously, removing the configured inbox tag outside the assistant
made background cleanup close that review, but an uploader-confirmed metadata save did not update
the workflow tags. Coupling tag changes to suggestion loading, refresh, rendering, cancellation,
or close would incorrectly treat observation or abandonment as review completion.

Paperless metadata and Discord cleanup are separate systems. Removing the inbox tag before
Discord acknowledges the save creates a race in which background polling can delete the review
thread before the success response is delivered. A failed or ambiguous tag write after Paperless
has accepted metadata also cannot be reported as though nothing changed.

## Decision

After Paperless re-fetches and confirms the uploader-selected metadata update, the application
uses the durable job uploader's linked Paperless credential to finalize review tags. It resolves
the exact configured `CLEANUP_INBOX_TAG` and optional `AI_REVIEW_COMPLETION_TAG` from the
uploader-visible taxonomy. Each configured name must have exactly one normalized exact match; the
assistant does not use partial matching and never creates the completion tag.

The final state excludes the inbox tag and includes the optional completion tag. Already-absent
and already-present states are idempotent successes. When a change is required, one Paperless
`modify_tags` operation carries both the add and remove sets, preserving every unrelated tag. The
application then re-fetches the document and verifies the intended state.

Transport failures and non-definitive server failures make the mutation outcome ambiguous. The
assistant never repeats that unsafe request automatically. It first re-fetches with the same
uploader credential and accepts success only if the observed state confirms the intended result;
otherwise Discord asks for reconciliation before another explicit Save.

SQLite records a per-item finalization cleanup gate. The gate blocks inbox-tag cleanup while the
operation is pending or failed. Individual Save opens it only after delivering its success
response. Save All waits for its aggregate response before opening every successful item's gate.
Existing batch resolution remains authoritative, so shared source and summary artifacts cannot
become eligible until every item is resolved. Partial Discord deletion remains retryable through
`/clean`.

Audit events contain principal and durable identifiers plus a fixed outcome code. They exclude tag
names, document content, and upstream error bodies. If metadata succeeds but finalization fails,
Discord explicitly distinguishes those outcomes and retains the review evidence.

Refresh, Retry Review, reset/cancel, Finish & Close without Save, recovery rendering, and
background polling do not initiate tag finalization.

## Consequences

- Operators may configure `AI_REVIEW_COMPLETION_TAG`; blank remains disabled.
- Both configured tags must already exist and be uniquely visible to each uploader expected to
  save reviews.
- The uploader needs current document tag-edit permission in addition to the existing metadata
  permissions.
- A finalization failure can leave confirmed metadata in Paperless while the Discord review stays
  open for retry or reconciliation.
- Rollback is manual and explicit: restore the inbox tag and remove the completion tag in
  Paperless. Deleted Discord artifacts are not recreated.

## Alternatives considered

- Finalize on suggestion load or refresh: rejected because reads do not confirm review completion.
- Use the system token when a user credential is missing or revoked: rejected because it violates
  owner-scoped authorization and least privilege.
- Create a missing completion tag automatically: rejected because configuration is not uploader
  authorization to expand taxonomy.
- Retry the bulk tag POST after a timeout: rejected because the first request may have succeeded.
- Mark the item cleanup-ready before replying in Discord: rejected because polling could delete
  the evidence before the uploader sees the result.
