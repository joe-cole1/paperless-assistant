# ADR 0012: Use durable per-file upload reviews and explicit batch resolution

Status: Accepted

Date: 2026-07-28

Issue: #79

Supersedes: ADR 0011's shared multi-document thread and thread-deleting **Finish & Close**
decisions, plus issue #10's immediate source-message cleanup assumption

## Context

A Discord message can be the starter for only one message-attached thread. Reusing that thread for
every attachment made multi-file reviews difficult to scan, gave only the first successful
document an obvious Paperless link, and let one **Finish & Close** action remove every document's
review. Deleting the user's source upload as soon as processing completed also removed the shared
batch anchor before the new review workflow was finished.

Posting one bot parent per attachment permits one public thread per document while leaving a
bounded, useful metadata record in the uploads channel when threads are collapsed or archived.
The source message and batch summary are shared artifacts, so neither can be safely deleted based
on one document's state.

## Decision

The assistant creates a durable upload batch and one ordered item before processing attachments.
It posts an immediate batch summary, then serially creates one bot-authored rich parent and public
thread per attachment as outcomes become reviewable. Each rich parent contains bounded status,
filename, Paperless ID/link when available, and current and pending title, date, correspondent,
document type, storage path, and tags. It never contains OCR text or document content.

Each successful item owns its review controls and **Open Paperless** link. **Save** writes only that
document and keeps its thread open. **Finish & Close** archives and locks only that thread while
retaining and updating the rich parent. Terminal failures expose an uploader-only, confirmed
**Dismiss Failed Upload** action. Reconciliation-required or otherwise uncertain items cannot be
dismissed or automatically resubmitted.

The batch summary exposes:

- **Refresh All**, which refreshes successful reviews;
- **Save All**, which confirms once and serially applies pending changes in original attachment
  order without closing threads;
- **Close All**, which confirms once, warns about discarded local choices, and closes successful
  reviews only. It never dismisses failures or resolves uncertain items.

Scalar selectors identify the current Paperless value when the user keeps it. Tags accept
repeatable one-at-a-time custom names. New names remain pending until an explicit save and continue
to use exact matching, ambiguity rejection, uploader credentials, current permissions, freshness
checks, idempotent creation, audit, and write verification.

The source upload and batch summary are deleted together as soon as every successful item is
closed, every terminal failure is manually dismissed, and no active or uncertain item remains.
A dismissed failure is resolved for this predicate. Per-file rich parents and archived threads are
retained. Cleanup is idempotent and records each shared artifact only after Discord confirms its
deletion or absence.

SQLite stores the batch, ordered items, job/document links, per-file parent/thread identifiers,
states, failure detail, and shared cleanup confirmations. Recovery rebuilds the appropriate
successful, failed, or uncertain per-file control surface. Inbox-tag removal closes matching
successful items; scheduled cleanup and `/clean` consult the same durable graph and never remove
active or uncertain batch artifacts.

## Consequences

- Multi-file uploads are independently discoverable and usable on mobile.
- A validation or AI-review failure does not remove controls for successful siblings.
- Batch operations remain convenient without weakening per-document authorization or write
  checks.
- Up to ten attachments create additional Discord messages and threads. Creation is serialized,
  names are sanitized and bounded, and progress is reported in the shared summary.
- Rich parents intentionally retain metadata after closure. Operators must treat the uploads
  channel as private document metadata.
- Existing active shared threads keep their legacy controls; the additive schema applies to new
  batches and recovered jobs linked to it.

## Alternatives considered

- Keep one batch thread and add clearer headings: fewer Discord objects, but still couples
  navigation, links, lifecycle, and failures across documents.
- Create standalone public threads: rejected because bot-parented threads provide a durable,
  readable channel-history anchor and simpler artifact recovery.
- Delete shared artifacts immediately after Paperless processing: rejected because processing
  success is not review completion.
- Let **Close All** dismiss failures: rejected because acknowledging a failed upload is a distinct
  user decision.
- Create tags immediately from the modal: rejected because typing a name is not authorization to
  write to Paperless.
