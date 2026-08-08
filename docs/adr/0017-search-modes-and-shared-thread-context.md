# ADR 0017: Use shared encrypted thread transcripts and restart-safe result sessions

Status: Accepted

Date: 2026-08-08

Issues: #41, #42

## Context

The questions channel needs both predictable Paperless search modes and useful follow-up
conversation. A message in the questions parent creates a public thread, while later messages in
that thread must share enough context to form a coherent household conversation. Result cards and
their controls can outlive a Discord process, so in-memory context cannot safely authorize
navigation, delivery, or similarity actions after a restart.

The assistant must remain read-only and fail closed. Raw questions, answers, document content,
and Paperless error bodies are private data. Controls must not let one participant act through
another participant's linked Paperless identity. Existing deployments already provide an encrypted
SQLite store, a Fernet key, and a short-lived context retention setting.

## Decision

Provide `/search rag`, `/search text`, `/search title`, `/search advanced`, and `/search reset` in
the configured questions surface. A command in the parent channel creates a public thread; a
command within one reuses it. Free-form questions use implicit RAG with full-text fallback. An
explicit `/search rag` request never falls back. Advanced mode forwards only Paperless native
`query` syntax and returns fixed safe validation guidance for invalid syntax.

Persist one bounded, encrypted transcript per guild and public questions thread using the existing
Fernet key and context TTL (15 minutes by default). The transcript uses generic `Participant`
labels and is shared among allowlisted participants, but each new request loads the current
asker's linked Paperless token. Bound stored prior-answer excerpts and discard whole oldest turns
when necessary; reject a current question over 4,000 characters rather than truncating it. Commit
a transcript turn only after the Discord answer UI has rendered successfully. Any allowlisted
participant may reset the transcript; reset does not invalidate prior result sessions.

Persist a separate requester-owned result session for the same TTL. It stores only the document
IDs, page state, and exact Discord message IDs needed to render and authorize controls. A session
contains at most 30 documents and displays three reusable cards per page. Prev, Next, Send File,
and Similar require the original requester, correct guild/thread/card, and an unexpired session.
This state makes valid controls restart-safe without storing raw query text or document content.

Legacy document-reference contexts are not migrated. They are removed during initialization or
allowed to expire under their existing retention behavior.

## Consequences

- Conversations are coherent for allowlisted household participants without attributing prior
  text to a Discord identity in the stored prompt.
- A participant cannot use another participant's result controls or Paperless token, even though
  the conversation itself is shared.
- SQLite stores more short-lived encrypted state and cleanup needs exact prompt, card, and
  navigation message IDs.
- A rendering failure leaves no new transcript turn or result session, avoiding durable state that
  does not match the visible Discord UI.
- Result controls survive a process restart until TTL expiry; stale, malformed, cross-thread, and
  cross-user controls fail safely.
- Explicit mode selection is predictable: only free-form implicit RAG may use text fallback.

## Alternatives

- Keep only an in-memory transcript and result context: rejected because restarts would turn
  visible controls into unauthorizable or unsafe interactions.
- Make transcripts requester-owned: rejected because a household thread should support shared
  follow-ups; requester ownership remains appropriate for credential-bearing result controls.
- Put the query or document content in component identifiers or SQLite session records: rejected
  because those values are private and unnecessary for authorization.
- Always fall back from RAG to text search: rejected because `/search rag` must give users a
  reliable explicit mode choice.
- Preserve and migrate legacy reference contexts: rejected because their ownership and payload
  model does not meet the new durable-session contract.
