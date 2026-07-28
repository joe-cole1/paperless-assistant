# Paperless Assistant architecture

Status: Discord-first MVP hardened by issue #65
Last updated: 2026-07-28

## 1. Purpose

Paperless Assistant provides a private household Discord experience over Paperless-ngx. Its target
capabilities are native Paperless document chat, referenced document delivery, and immediate
multi-file ingestion with uploader-reviewed Paperless AI metadata suggestions. Paperless owns OCR,
classification, storage, search, LLM execution, and AI configuration.
The assistant owns Discord authorization, workflow policy, reliability, privacy-minimized audit,
and delivery decisions.

Issue #9 established the private runtime after retiring Gemini Spark/MCP. Issue #10 added the
outbound Discord worker, Paperless adapter, application services, and durable local state. Issue
#65 hardens authorization, credentials, diagnostics, cleanup, CI, and Paperless 3.0.4 AI review.

## 2. System context

```mermaid
flowchart LR
    Users["Allowlisted household users"]
    Discord["Private Discord guild"]
    Assistant["Paperless Assistant"]
    Paperless["Paperless-ngx v3"]
    Provider["Paperless-configured AI provider"]
    State["SQLite and restricted staging"]

    Users --> Discord
    Discord <-->|"Outbound Gateway connection"| Assistant
    Assistant -->|"Native authenticated APIs"| Paperless
    Paperless -->|"Configured by Paperless"| Provider
    Assistant --> State
```

Discord, Paperless, and state flows are implemented in issue #10.

## 3. Runtime and exposure

One `paperless-assistant` container is the primary runtime. Discord connects outbound through the
Gateway. There is no public webhook, HTTP interaction, MCP, OAuth resource, Spark, or Pangolin
route.

The ASGI health surface exposes:

- `/healthz`: liveness while the process can serve;
- `/readyz`: lifecycle and dependency-policy readiness;
- `/`: non-secret service metadata.

Compose binds optional host access to NAS loopback only. Container health checks use the
container-local listener.

## 4. Ports and adapters

- inbound Discord adapter: validates transport identity, auto-creates public threads on top-level
  user messages, renders references and AI review controls, and enforces uploader ownership on
  every metadata-write interaction;
- application services: authorize users and enforce query, delivery, ingestion, suggestion
  freshness/tag merging, batch inbox polling, and confirmed cleanup policy;
- Paperless gateway: the only component that performs Paperless HTTP calls, including the
  synchronous 3.0.4 AI suggestion trigger and bounded diagnostic logging;
- ingestion and audit repositories: durable SQLite implementations tracking exact message/channel
  cleanup targets and confirmation state;
- delivery adapter: stages and sends Discord files or returns authenticated Paperless links.

Domain and application code do not import Discord or HTTP client implementations.

## 5. Trust boundaries

1. **Discord to assistant:** messages, attachments, IDs, and interactions are untrusted until the
   exact guild, channel, and immutable user ID are authorized.
2. **Assistant to Paperless:** each user token and the system token are privileged credentials.
   Tokens are encrypted with a generated Fernet key and never cross into Discord, logs, audit,
   URLs, or local filenames. Revocation fails queued work closed without a system-token fallback.
3. **Paperless to its AI provider:** Paperless owns this configuration and data flow. The
   assistant invokes only Paperless's native APIs and holds no model-provider credential.
4. **Writable state:** SQLite, staged uploads, and delivery spools are restricted, bounded, and
   retention-managed. The root filesystem remains read-only.
5. **Discord retention:** questions, answers, status messages, and source attachments exist in
   Discord until policy cleanup removes them.

## 6. Security invariants

- Default deny for Discord guild, channel, user, DM, thread, bot, webhook, and edited-message
  routing.
- No public application route.
- No MCP, OAuth/JWT/JWKS, Spark, or Pangolin dependency.
- No taxonomy creation, document delete, unrestricted metadata update, or user administration
  operation in application code.
- No tokens, authorization headers, document bytes, or audit payloads containing user content.
  Discord receives only generic Paperless failures. Restricted server logs may contain a bounded,
  JSON-escaped, credential-redacted Paperless error body for operator diagnosis.
- AI metadata writes require the original uploader, a fresh document-state check, serialized apply,
  bounded controls, and preservation of existing tags. Unmatched taxonomy names are never created.
- Cleanup is fail-closed: Paperless batch failures do not delete Discord messages, and database
  evidence is purged only after Discord confirms deletion or absence at the exact channel/message.
- Unsafe POST requests are never retried after an ambiguous result.
- Container remains non-root, read-only, capability-free, and `no-new-privileges`.

## 7. Configuration

Configuration includes validated Discord, Paperless, persistence, limits, timeouts, retention,
and timezone policy.

Secrets are runtime-injected through a permission-restricted deployment environment and never
placed in source, image layers, tests, documentation examples, issues, or pull requests.

## 8. Persistence and recovery target

SQLite WAL storage holds ingestion jobs, Discord event idempotency, short-lived
document-reference context, exact cleanup channel/message targets and confirmations, the
missing-tag warning ID/timestamp, encrypted per-user tokens, and privacy-minimized audit. Staged
files use job UUID names and restrictive permissions.

The target state machine is:

```text
staged -> submitting -> submitted -> succeeded | failed
                  \-> reconciliation_required
```

A saved Paperless task UUID resumes polling after restart. A crash in the ambiguous upload window
never automatically resubmits. Identical content in different Discord messages remains allowed;
only repeated processing of the same Discord event/job is suppressed.

## 9. Availability

Liveness is independent of Discord, Paperless, or the AI provider. Readiness represents whether
the worker can safely serve its configured capabilities. A missing required source tag disables
ingestion and degrades readiness without disabling search/download. Transient Paperless or AI
outages return bounded user-facing failures and must not cause destructive restart loops.

## 10. Deployment

The target is a Synology NAS using Docker Compose. The image is immutable except for a restricted
Docker-managed data volume whose stable Compose name survives container recreation and stack
renaming. A host bind mount remains an advanced operator option. Health access is loopback-only.
One worker replica is supported; horizontal scaling requires a new coordination and storage
design.

Deployment retains the previous known-good image and configuration for rollback. Database schema
changes use forward migrations and backups. Staged data is transient and excluded from backups;
SQLite job/audit state is included according to operator policy.

## 11. Decisions and roadmap

- ADR 0001: Python and uv remains accepted.
- ADR 0002: ports-and-adapters modular monolith remains accepted.
- ADR 0003: MCP transport is superseded.
- ADR 0004: phased, fail-closed capability delivery remains accepted.
- ADR 0005: separate Discord-worker assumptions are superseded in part.
- ADR 0006: Discord is the primary runtime.
- ADR 0007: native Paperless chat and immediate durable ingestion.
- ADR 0008: a Docker-managed volume is the zero-setup persistence default.
- ADR 0009: fail-closed AI metadata review, diagnostics, cleanup, credentials, and CI writeback.

Issue #10 is the canonical Discord MVP. Issue #20 corrects its default storage deployment. Issue
#65 is the July 2026 hardening and AI-suggestions correction. Issue #8 remains the broader
least-privilege Paperless permissions phase. A future inbound
interface, custom AI layer, public share-link capability, or additional user population requires
a separate issue and security review.
