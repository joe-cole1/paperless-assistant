# ADR 0013: Make review rendering idempotent and delete resolved artifacts

Status: Accepted

Date: 2026-07-29

Issue: #81

Supersedes: ADR 0012's decision to retain rich parents and archived threads after resolution

## Context

The issue #79 implementation stored each document parent and thread but not the title, metadata,
actions, or thread-control message IDs inside it. Initial upload completion and the recovery loop
could therefore render the same successful review concurrently. Recovery also had no durable way
to distinguish the canonical control surface from a duplicate.

Retaining closed and dismissed parents kept useful history, but it also left resolved document
artifacts in the uploads channel after the user had explicitly finished the workflow. Operators
need a bounded cleanup command that can retry partial deletion and remove genuine bot-owned
orphans without treating arbitrary Discord content as disposable.

## Decision

SQLite stores the four successful-review child message IDs and independent confirmation flags for
per-file parent and thread deletion. A process-local lock serializes initial and recovery rendering
for each source-message/attachment pair. Rendering edits stored messages and creates a replacement
only when an expected message is absent. Batch controllers reject duplicate per-item controllers.

**Finish & Close**, **Close All**, inbox-tag closure, and **Dismiss Failed Upload** first persist
the resolved item state, then attempt to delete its thread and parent. Thread and parent deletion
are confirmed independently. A Discord failure leaves the unconfirmed artifact eligible for a
later `/clean` retry without reopening or repeating the Paperless operation. The shared source and
batch summary still become eligible only when every item is resolved.

In the uploads channel, `/clean`:

- retries tracked parent/thread deletion for resolved items;
- retries shared source/summary deletion for fully resolved batches;
- removes bot-owned upload parents or document threads that have no durable item;
- removes superseded, recognizable bot review-surface messages inside a tracked active thread;
- preserves active, failed-undismissed, processing, reconciliation-required, user-authored,
  pinned, unrelated, or ambiguously identified content.

## Consequences

- Initial completion and recovery converge on one durable review surface.
- Resolved documents no longer leave metadata parents or archived threads in channel history.
- Cleanup remains fail-closed and retryable across non-transactional Discord/database operations.
- Strict ownership, naming, content, and durable-ID checks limit orphan cleanup to artifacts
  created by this workflow.
- The bot requires **Manage Threads** and **Manage Messages** to complete cleanup promptly.
