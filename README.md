# Paperless Assistant

Paperless Assistant is a private, self-hosted Discord interface for a household
Paperless-ngx v3 deployment. Allowlisted users can ask ordinary English questions, receive Paperless's native AI answer and referenced files, and immediately ingest several mobile attachments.

Each user securely links their own Paperless API token using the `/auth link <token>` ephemeral
command. The token is validated against Paperless and saved in SQLite encrypted with the generated
Fernet key in `ENCRYPTION_KEY`.

Paperless owns OCR, classification, search, storage, and AI configuration. The assistant adds no
model, embeddings, query planner, or RAG layer.

For a complete first installation—from creating the private Discord bot through manual acceptance
testing—follow [docs/INSTALL.md](docs/INSTALL.md).

## User experience

Configure two private Discord text channels:

- **questions** — ask “give me John's vaccine records from 2024” or “find the receipts from our
  Venice trip.” The bot auto-creates a dedicated public thread for each query. Paperless native chat returns up to three references rendered as rich Discord embed cards inside the thread with Open, Send File, and Dismiss controls, and thread-scoped follow-up context.
- **uploads** — attach up to ten documents. Top-level uploads auto-create a dedicated thread where
  status updates and Paperless processing progress are reported. After consumption, the bot asks
  Paperless 3.0.4 to run its LLM-backed suggestions and presents the proposed title,
  correspondent, document type, storage path, date, and tags for review. Only the uploader can
  apply them. Applying rechecks the document, preserves existing tags, and patches its metadata;
  unmatched names are displayed but never auto-created. Once the configured
  `CLEANUP_INBOX_TAG` (default `inbox`) is removed from a document in Paperless, the bot
  automatically deletes the corresponding upload notification message and thread.

Slash commands available to users:

- `/auth link <token>` — Ephemerally test and store your Paperless API token.
- `/auth unlink` — Remove your linked Paperless API token.
- `/auth status` — Check whether your Paperless account token is linked and valid.
- `/clean [count]` — Bulk-purge assistant messages on demand in assistant channels.

If an original exceeds Discord's effective attachment limit, the assistant offers an archived PDF
when it fits and always provides the session-authenticated original Paperless download link.

## Architecture and exposure

One container connects outbound through the Discord Gateway. Discord is the only user interface.
There is no public webhook, MCP, OAuth, Spark, or Pangolin route.

The private HTTP surface contains only:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Non-secret service metadata |
| `GET` | `/healthz` | Process liveness |
| `GET` | `/readyz` | Discord and ingestion-policy readiness |

Compose binds optional host monitoring to `127.0.0.1:8780`. Do not publish or reverse-proxy it.
See [architecture.md](architecture.md) and [docs/adr](docs/adr).

## Prerequisites

- Paperless-ngx v3.0.4 with native AI enabled and an LLM backend configured
- one exact, unique visible Paperless tag named `Discord` (configurable)
- a system Paperless API token in `.env` (used for background system tasks such as taxonomy polling and inbox tag cleanup)
- a private Discord guild, bot application, questions channel, and uploads channel
- Docker Engine with Docker Compose v2
- for development: Python 3.14 and uv 0.11.31

## Discord bot setup

Enable the privileged **Message Content Intent** in the Discord Developer Portal. Invite the bot
only to the private guild and grant the two configured channels:

- View Channel
- Send Messages
- Read Message History
- Attach Files
- Embed Links
- Manage Messages

Record the immutable guild and channel IDs with Discord Developer Mode. The assistant
default-denies DMs, threads, other guilds/channels, bots, webhooks, edited messages, and
unauthenticated users. `DISCORD_ALLOWED_USER_IDS` is mandatory and must contain at least one
immutable user ID; an empty allowlist prevents startup.

## Configure and deploy

```console
cp .env.example .env
chmod 600 .env
docker compose build
docker compose up -d --wait
docker compose ps
```

Replace every example ID, URL, and token in `.env`. Important settings:

- `PAPERLESS_INTERNAL_URL` — private container/LAN API URL
- `PAPERLESS_PUBLIC_URL` — HTTPS URL users open in a browser
- `PAPERLESS_AI_SUGGESTIONS_TIMEOUT_SECONDS=150` — client bound for synchronous Paperless AI
  suggestions; keep it above Paperless's `PAPERLESS_AI_LLM_REQUEST_TIMEOUT`
- `SUGGESTION_REVIEW_TIMEOUT_SECONDS=900` — lifetime of uploader-only review controls
- `PAPERLESS_OFFICE_UPLOADS_ENABLED=false` — upload policy flag only
- `CLEANUP_INBOX_TAG=inbox` — tag monitored for removal to auto-delete upload notifications
- `CLEANUP_INBOX_TAG_POLL_INTERVAL_SECONDS=300` — polling frequency for tag removal checks
- `CLEANUP_QUESTION_DELAY_MINUTES=0` — optional auto-delete delay for Q&A pairs (0 = daily 03:00 purge)
- `CLEANUP_UPLOAD_DELAY_MINUTES=0` — optional auto-delete delay for upload notifications
- `TZ` — container timezone used for the 03:00 cleanup

Tika and Gotenberg remain part of the existing Paperless deployment. Set
`PAPERLESS_OFFICE_UPLOADS_ENABLED=true` only when they work there. A bad Tika/Gotenberg setup may
fail an Office task, but PDF/image questions and ingestion remain available.

The Docker-managed `paperless-assistant-data` volume holds SQLite recovery, idempotency, and audit
state plus transient staging/delivery files. Container recreation and `docker compose down`
preserve it. Deleting the volume—including with `docker compose down -v`—discards that state;
documents already accepted by Paperless remain in Paperless. See the installation guide for
optional backup and advanced host bind-mount procedures. A bind mount requires the host directory
to be created, assigned to UID/GID `10001:10001`, restricted to mode `0700`, and verified before
startup; copying the supplied override alone is not sufficient. The deployment supports exactly
one worker replica.

The container runs as UID 10001 with a read-only root filesystem, a restricted writable data
mount, bounded `/tmp`, no Linux capabilities, and `no-new-privileges`. Default resources are
512 MiB and 1 CPU.

## Health and operations

```console
python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8780/healthz')))"
python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8780/readyz')))"
docker compose logs -f paperless-assistant
```

Liveness stays healthy through downstream outages. Readiness is degraded when Discord is
disconnected or the exact required Paperless source tag cannot be verified. A missing tag pauses
uploads, posts a bounded warning, and rechecks every five minutes; questions and downloads remain
usable. The daily warning bound survives restarts, and the recorded warning is removed when the
tag becomes healthy.

Uploads are durable in SQLite. A saved task UUID resumes polling after restart. An interrupted
ambiguous upload POST is marked `reconciliation_required` and is never automatically repeated.
Nightly cleanup follows the container timezone and never deletes active or reconciliation jobs.
Paperless HTTP and task errors remain generic in Discord. Server logs include a bounded,
control-character-safe, credential-redacted Paperless diagnostic plus operation and status code;
they never include authorization headers.

Paperless 3.0.4 exposes two different suggestion systems. This assistant deliberately calls
`GET /api/documents/{id}/ai_suggestions/`, which synchronously invokes Paperless's configured LLM.
It does not silently fall back to the classic classifier endpoint
`GET /api/documents/{id}/suggestions/`. Embeddings are optional for per-document suggestions; when
configured, Paperless can use its LLM index to ground suggestions in similar documents.

## Supported uploads

Always enabled after signature validation:

- PDF, PNG, JPEG, TIFF, GIF, WebP, and UTF-8 plain text

Enabled only by `PAPERLESS_OFFICE_UPLOADS_ENABLED=true`:

- DOC/DOCX/ODT, PPT/PPTX/ODP, XLS/XLSX/ODS, and EML

HEIC/HEIF, archives, unsupported types, and extension/signature mismatches are rejected with
actionable guidance. The incoming default is 25 MiB per file and 100 MiB total staged data.

## Development and verification

```console
uv python install 3.14
uv sync --locked
./scripts/ship_check.sh
```

Pull-request CI runs Ruff fixes and formatting without credentials, validates the resulting tree,
and, for branches in this repository, commits only the validated Python formatting patch back to
the PR after all checks pass. Fork PRs remain read-only and receive a clear formatting failure.

Tests use only synthetic identities and documents. Never commit `.env`, tokens, private documents,
OCR, questions, answers, captions, filenames, titles, or taxonomy values.

## Releases

Every pull request must have at least one label. GitHub uses the categories in
`.github/release.yml` to group merged pull requests in generated release notes.

After the release commit is merged and CI succeeds, push an annotated semantic-version tag:

```console
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

The release workflow publishes `ghcr.io/joe-cole1/paperless-assistant` for the same architectures
as the supported Paperless-ngx v3.0.4 image: `linux/amd64` and `linux/arm64` (ARMv8). It attaches
provenance and an SBOM, then creates the GitHub release. Stable releases also update the major,
major/minor, and `latest` image tags. Prerelease tags such as `v0.2.0-rc.1` do not update
`latest`.

Deploy an immutable release by pinning its digest from the release notes:

```console
docker pull ghcr.io/joe-cole1/paperless-assistant@sha256:REPLACE_WITH_RELEASE_DIGEST
```

## Roadmap

1. MCP retirement and private health foundation — issue #9.
2. Discord-native chat, delivery, and immediate multi-file ingestion — issue #10.
3. Least-privilege Paperless identities and object permissions — issue #8.

See [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), and [LICENSE](LICENSE).
