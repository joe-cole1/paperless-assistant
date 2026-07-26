# ADR 0007: Use native Paperless chat and durable immediate Discord ingestion

- Status: Accepted
- Date: 2026-07-25
- Depends on: ADR 0006
- Superseded in part by: ADR 0008
- Issue: #10

## Context

The household's primary need is ordinary-language document retrieval for non-technical users,
plus convenient mobile ingestion. Paperless-ngx v3 already owns an AI-backed document chat,
full-text search, OCR, classification, task tracking, notes, original/archive downloads, and
duplicate handling. Adding another LLM or retrieval index would duplicate policy, credentials,
data exposure, and future Paperless improvements.

Discord attachment delivery and Paperless ingestion can outlive one Gateway connection. An upload
POST can also fail after Paperless accepted bytes but before the client received its task UUID.

## Decision

Use one outbound Discord Gateway client with exact guild, channel, and immutable user-ID
allowlists. Free-form questions pass unchanged to `POST /api/documents/chat/`; the assistant
parses Paperless's final metadata trailer and accepts at most its three native references. Native
full-text search receives the unchanged question only when chat is unavailable or has no content.
No external AI client, embeddings, RAG, query planner, or taxonomy inference exists here.

Use two private channels: questions and uploads. Upload attachments are immediate confirmation.
Validate signatures, stage under UUID paths, resolve only exact unambiguous existing taxonomy
names, add the required existing source tag, submit serially, and poll saved Paperless task UUIDs.
The caption never sets a title and becomes one idempotently checked Paperless note after success.

Persist workflow state, event identity, short-lived ordered reference IDs, and minimized audit in
SQLite WAL on a restricted writable volume. A compare-and-swap state machine prevents duplicate
event submission. A crash in `submitting` becomes `reconciliation_required`; it never
automatically repeats the POST. A saved task UUID resumes polling.

Use the effective Discord delivery limit at interaction time. Prefer the latest original,
fall back to an archived PDF attachment when it fits, and otherwise provide the user's
session-authenticated original download URL. Never create public share links or ZIP bundles.

## Consequences

Paperless remains the source of truth for document answers, result count, authorization,
classification, duplicates, and processing. The Discord worker benefits from future native API
improvements without maintaining a parallel AI layer.

The MVP uses one operator-supplied admin Paperless token in a trusted two-user deployment. This is
a known broad credential; issue #8 owns migration to least-privilege identities and object
permissions.

SQLite and staged files require one writable bind mount, backup/permission procedures, and a
single worker replica. Ambiguous uploads require deliberate Paperless reconciliation instead of
automatic convenience retries.

## Alternatives considered

- Add a second LLM/RAG layer: rejected because it duplicates Paperless and expands private-data
  exposure.
- Require slash commands: rejected because ordinary language is the primary household interface.
- Return only links: rejected because small originals are more convenient as Discord attachments.
- Retry ambiguous uploads: rejected because it can create unintended duplicate ingestion.
- Run Tika/Gotenberg here: rejected because they are Paperless deployment services; this project
  only gates dependent upload formats with a configuration flag.
