# ADR 0011: Make Discord AI review configurable and keep thread controls persistent

Status: Accepted

Date: 2026-07-28

Issue: #77

Supersedes: ADR 0010's requirement that every unmatched taxonomy creation use a second prompt

## Context

The combined AI review introduced by issues #44 and #70 displayed the selected metadata twice:
once as a summary and again in unlabeled closed selectors. Title and date shared a modal, and
document-level actions were mixed into the review card. Operators also could not remove fields
that their household should not edit. Every unmatched taxonomy selection required a second
confirmation even when a deployment wanted the uploader's explicit **Apply Changes** action to be
the confirmation.

Discord select placeholders remain visible while a menu is closed, so they can identify the field
without a duplicate summary. A review may contain pending choices before any Paperless write, and
refreshing the Paperless response or deleting the upload thread can discard those choices.

## Decision

Each document review is rendered as up to three focused messages:

- a title message with **Edit Title**;
- an editable-metadata message with labeled Date, Correspondent, Document Type, Storage Path, and
  Tags selectors;
- an action message with **Apply Changes** and **Reset Changes**.

The upload status message retains owner-only **Open Paperless**, **Refresh**, and **Finish &
Close** controls for the lifetime of the review. Refresh reads the supported Paperless AI
suggestions endpoint and remains honest about cache behavior. Finish & Close deletes the complete
Discord thread. Refresh and close require a discard confirmation when any document session differs
from its last applied selection; otherwise they proceed directly.

`ALLOW_EDIT_TITLE`, `ALLOW_EDIT_DATE`, `ALLOW_EDIT_CORRESPONDENT`,
`ALLOW_EDIT_DOCUMENT_TYPE`, `ALLOW_EDIT_STORAGE_PATH`, and `ALLOW_EDIT_TAGS` default to `true`.
A disabled field is absent from Discord and rejected again by the application service, including
taxonomy resolution and creation.

`REQUIRE_NEW_METADATA_CONFIRMATION` defaults to `true`. When true, selected unmatched taxonomy
names receive the existing second prompt. When false, the uploader's **Apply Changes** interaction
also authorizes creation. Both paths still perform exact-name lookup, fail on ambiguity, check the
linked user's current Paperless permission, create idempotently, record a privacy-minimized audit
event, compare the document freshness baseline, and verify the final write.

## Consequences

- Closed selectors identify their field and current pending choice without a duplicate checkbox
  summary.
- Deployments can reduce the editable surface without relying on UI hiding as authorization.
- Thread-destructive and refresh actions are available from a stable location and do not silently
  discard pending choices.
- Disabling the second prompt reduces interaction count but does not bypass Paperless permissions,
  exact matching, freshness, audit, or verification.
- A multi-document upload has one thread controller and one independent review session per
  successfully processed document.

## Alternatives considered

- Keep the duplicate selected-values summary: rejected because the labeled selectors already
  communicate the editable state and the duplicate made the workflow harder to scan.
- Treat hidden fields as a Discord-only preference: rejected because stale or forged component
  interactions must fail closed at the application boundary.
- Always require the second creation prompt: retained as the default but rejected as mandatory
  policy because the explicit Apply Changes action can be an operator-approved confirmation point.
- Delete the thread without checking pending state: rejected because Discord selections are local
  until Paperless confirms Apply Changes.
