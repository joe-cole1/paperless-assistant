# Install and test Paperless Assistant

This guide walks through a private household installation from an existing Paperless-ngx v3
server to a working Discord bot. It assumes Docker Engine, Docker Compose v2, Git, and a private
Discord server.

The assistant does not expose a Discord webhook or public application endpoint. It connects
outbound to Discord, calls Paperless through its API, and binds health monitoring only to
`127.0.0.1`.

## 1. Check Paperless first

Before creating the bot:

1. Sign in to Paperless 3.0.4 as an administrator.
2. Confirm AI suggestions work in Paperless itself: open a synthetic or non-sensitive test
   document, select **Suggest**, request AI-assisted suggestions, and verify that an LLM-generated
   title or metadata proposal appears. Classic classifier suggestions alone are not sufficient.
3. Create exactly one Paperless tag named `Discord`.
   - The name is configurable, but it must be unique when compared case-insensitively.
   - Do not create both `Discord` and `discord`.
4. Open the user menu, select **My Profile**, and generate or regenerate the API token with the
   circular-arrow control.
5. Put the token directly into a password manager. Do not paste it into an issue, shell command,
   log, screenshot, or chat transcript. The assistant's ephemeral `/auth link` command is the one
   supported Discord entry point.
6. Record two Paperless addresses:
   - an internal address reachable from the assistant container;
   - the HTTPS address your household uses in a browser.

If both addresses are the same HTTPS URL, that is functional. A private Docker/LAN address for API
traffic is preferred. A Docker service name such as `paperless-webserver` works only when the
assistant container can resolve and reach that service on a shared Docker network.

Office and email ingestion is optional. Leave it disabled unless the existing Paperless deployment
already has working Tika and Gotenberg services.

Paperless documents the profile token workflow and API authentication in its
[REST API guide](https://docs.paperless-ngx.com/api/).

### Required Paperless 3.0.4 AI configuration

AI suggestions are opt-in, synchronous LLM requests in Paperless 3.0.4. Configure them in
**Settings → Application Configuration** or with Paperless environment variables. Database
application settings take precedence over environment values.

At minimum Paperless needs:

```dotenv
PAPERLESS_AI_ENABLED=true
PAPERLESS_AI_LLM_BACKEND=ollama
PAPERLESS_AI_LLM_ENDPOINT=http://ollama:11434
PAPERLESS_AI_LLM_MODEL=llama3.1
PAPERLESS_AI_LLM_REQUEST_TIMEOUT=120
```

For an OpenAI-compatible provider, use `PAPERLESS_AI_LLM_BACKEND=openai-like` and configure the
provider's model, `PAPERLESS_AI_LLM_API_KEY`, and endpoint as required. A hosted provider receives
document content and may charge for requests; assess that privacy and cost before enabling it.

`PAPERLESS_AI_LLM_EMBEDDING_BACKEND` is optional for suggestions. When configured, Paperless's LLM
index can add similar-document context; it is required for collection chat, not for the
per-document AI suggestion request used after an upload.

## 2. Create the private Discord channels

In the household Discord server:

1. Create a private text channel named `paperless-questions`.
2. Create a private text channel named `paperless-uploads`.
3. Allow only the household members who should access private documents.
4. Keep both channels out of public categories, or explicitly disable **View Channel** for
   `@everyone`.

The bot will reject DMs, threads, other channels, bots, webhooks, other guilds, and users not
listed in its configuration.

## 3. Create the Discord application and bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Select **New Application**, name it `Paperless Assistant`, and create it.
3. Open **Installation**:
   - enable **Guild Install**;
   - user installation is not needed;
   - leave **Install Link** set to **None**—Discord does not provide an installation-page link
     for a private bot;
   - under the default guild install settings, add the `bot` scope if that control is available.
4. Open **Bot**:
   - disable **Public Bot** for this private installation;
   - enable **Message Content Intent** under privileged gateway intents;
   - do not enable Presence or Server Members intents;
   - select **Reset Token** (or the available token-generation control), copy the bot token once,
     and store it in the password manager.

Message Content intent is mandatory: without it Discord omits message text and attachments from the
events the worker consumes. Private, unverified household bots can enable the intent in the portal
without applying for verification. See Discord's
[bot setup guide](https://docs.discord.com/developers/quick-start/getting-started) and
[Message Content documentation](https://support-dev.discord.com/hc/en-us/articles/4404772028055-Message-Content-Privileged-Intent-FAQ).

Treat the bot token like a password. Reset it immediately if it appears in a screenshot, terminal
history, issue, or message.

## 4. Install the bot in the server

Keep **Public Bot** disabled. Only the application owner and members of its developer team can
install a private bot. Sign in to Discord as one of those accounts and use the bot OAuth2
authorization flow instead of the Installation page's install-link setting:

1. Open **General Information** and copy the **Application ID**. This ID is not the bot token.
2. Open **OAuth2 → URL Generator** and select both the `bot` and `applications.commands` scopes.
3. Grant:
   - View Channels
   - Send Messages
   - Send Messages in Threads
   - Create Public Threads
   - Manage Threads
   - Read Message History
   - Attach Files
   - Embed Links
   - Manage Messages
   - Use Application Commands
4. Copy and open the generated URL. If the Portal does not show a generated URL, replace
   `APPLICATION_ID` in this equivalent authorization URL:

   ```text
   https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&scope=bot%20applications.commands&permissions=328565124096
   ```

5. Choose **Add to server** and select the private household server.

Discord requires the installing account to have permission to manage the server. The bot does not
need Administrator.

After installation, review the two private channels' permission overrides. The bot role must have
the permissions above in both channels. **Create Public Threads**, **Send Messages in Threads**,
and **Manage Threads** allow the bot to create, populate, and delete resolved per-file reviews,
while **Manage Messages** lets it remove resolved parents and batches' shared source and summary
messages. Do not
grant it access to unrelated private channels.

Discord explains bot tokens, scopes, and least-privilege server permissions in its
[OAuth2 and permissions guide](https://docs.discord.com/developers/platform/oauth2-and-permissions).

## 5. Copy immutable Discord IDs

In Discord:

1. Open **User Settings → Advanced** and enable **Developer Mode**.
2. Right-click the server and select **Copy Server ID**.
3. Right-click `paperless-questions` and copy its channel ID.
4. Right-click `paperless-uploads` and copy its channel ID.
5. Right-click every allowed household member and copy their immutable user ID.

`DISCORD_ALLOWED_USER_IDS` is mandatory and must not be empty. Restrict bot interactions to the
specific users with a JSON array:

```dotenv
DISCORD_ALLOWED_USER_IDS=[111111111111111111,222222222222222222]
```

## 6. Install the assistant

Choose a private directory for the Compose project:

```console
git clone https://github.com/joe-cole1/paperless-assistant.git
cd paperless-assistant
cp .env.example .env
chmod 600 .env
```

Compose creates the persistent `paperless-assistant-data` volume with the ownership required by
the non-root container. No host data directory, SSH permission change, or Synology UID/GID setup
is required. In Synology Container Manager, deploy `compose.yaml` as a Project; do not add a
second `/data` mount to the service.

### Optional advanced host bind mount

Use a host bind mount only when you deliberately want the assistant database visible to
host-managed backup tools. It restores a one-time Unix ownership requirement and is not needed
for normal installation.

Stop the service before changing its storage mapping. Create the directory on a local Synology
Btrfs/ext4 volume, then assign it to the container's numeric UID/GID and restrict it:

```console
sudo mkdir -p /volume1/docker/paperless-assistant/data
sudo chown -R 10001:10001 /volume1/docker/paperless-assistant/data
sudo chmod 700 /volume1/docker/paperless-assistant/data
sudo stat -c '%u:%g %a %n' /volume1/docker/paperless-assistant/data
```

The final command must report UID/GID `10001:10001` and mode `700`. Do not continue if it does
not. Avoid SMB/NFS-backed paths that reject normal Unix `chown` or `chmod`; use a local NAS volume
instead.

Enable the supplied override and replace its example `source` with the exact absolute directory
you prepared:

```console
cp compose.bind-mount.yaml.example compose.override.yaml
```

Compose automatically reads `compose.override.yaml`. Keep the mapping writable and targeted
exactly at `/data`. Do not set `PAPERLESS_ASSISTANT_DATA_PATH`; that obsolete variable is no
longer part of the deployment. After these steps, the normal build and startup commands below
use the bind mount.

## 7. Configure `.env`

Edit `.env` locally on the Docker host. At minimum, replace:

```dotenv
TZ=America/New_York

DISCORD_TOKEN=replace-with-discord-bot-token
DISCORD_GUILD_ID=123456789012345678
DISCORD_QUESTIONS_CHANNEL_ID=123456789012345679
DISCORD_UPLOADS_CHANNEL_ID=123456789012345680
DISCORD_ALLOWED_USER_IDS=[111111111111111111,222222222222222222]

ENCRYPTION_KEY=replace-with-generated-fernet-key

PAPERLESS_INTERNAL_URL=http://paperless-webserver:8000
PAPERLESS_PUBLIC_URL=https://paperless.example.invalid
PAPERLESS_TOKEN=replace-with-paperless-admin-token
PAPERLESS_SOURCE_TAG=Discord
PAPERLESS_OFFICE_UPLOADS_ENABLED=false
PAPERLESS_AI_SUGGESTIONS_TIMEOUT_SECONDS=150
SUGGESTION_REVIEW_TIMEOUT_SECONDS=900
ALLOW_EDIT_TITLE=true
ALLOW_EDIT_DATE=true
ALLOW_EDIT_CORRESPONDENT=true
ALLOW_EDIT_DOCUMENT_TYPE=true
ALLOW_EDIT_STORAGE_PATH=true
ALLOW_EDIT_TAGS=true
REQUIRE_NEW_METADATA_CONFIRMATION=true
AI_REVIEW_COMPLETION_TAG=

CLEANUP_INBOX_TAG=inbox
CLEANUP_INBOX_TAG_ENABLED=true
CLEANUP_INBOX_TAG_POLL_INTERVAL_SECONDS=300
CLEANUP_QUESTION_DELAY_MINUTES=0
CLEANUP_UPLOAD_DELAY_MINUTES=0
```

Important details:

- `ENCRYPTION_KEY` must be a newly generated URL-safe base64 Fernet key. Generate it once in a
  private terminal, store it in the password manager, and paste only the result into `.env`:

  ```console
  openssl rand -base64 32 | tr '+/' '-_'
  ```

  Existing tokens encrypted with the previous passphrase-derived scheme cannot be decrypted after
  this hardening change. Keep the database for job/audit continuity, set the new key, and have each
  user run `/auth link` again.
- `PAPERLESS_TOKEN` in `.env` is required for system background tasks (tag taxonomy polling and inbox tag cleanup).
- Each channel member links their personal Paperless account by typing `/auth link <token>` ephemerally in Discord once the bot is running.
- The linked Paperless user must be able to view and change the uploaded document; Paperless's AI
  suggestion endpoint enforces its owner-aware `change_document` permission.
- Matched existing metadata can be reviewed and applied with document-change permission alone.
  Creating a selected unmatched name additionally requires the linked user to have Paperless's
  corresponding add permission for tags, correspondents, document types, or storage paths. The
  assistant checks for an exact existing name again before creating anything.
- `PAPERLESS_INTERNAL_URL` must work from inside this container, not merely from the Docker host.
- `PAPERLESS_PUBLIC_URL` must be the HTTPS browser URL your users open.
- Do not add `Token ` before `PAPERLESS_TOKEN`; supply only the token value.
- Keep Office uploads `false` for the first startup.
- `CLEANUP_INBOX_TAG` configures the Paperless tag monitored for automatic Discord message removal once removed in Paperless.
- `PAPERLESS_AI_SUGGESTIONS_TIMEOUT_SECONDS` must be greater than Paperless's
  `PAPERLESS_AI_LLM_REQUEST_TIMEOUT`; the defaults are 150 and 120 seconds respectively.
- `SUGGESTION_REVIEW_TIMEOUT_SECONDS` bounds how long the uploader-only review and thread controls
  remain active.
- Every `ALLOW_EDIT_*` setting defaults to `true`. Setting one to `false` removes that field's
  Discord control and causes the application service to reject a forged or stale interaction that
  attempts to write it.
- `REQUIRE_NEW_METADATA_CONFIRMATION=true` keeps the default second prompt before creating
  selected unmatched tags, correspondents, document types, or storage paths. When set to `false`,
  **Apply Changes** is sufficient creation authorization; exact-name lookup, ambiguity failure,
  Paperless permission checks, freshness checks, audit, and write verification still run.
- `AI_REVIEW_COMPLETION_TAG` is optional and disabled when blank. When configured, it must name
  one exact, uniquely uploader-visible existing Paperless tag. The assistant never auto-creates it.
- After Paperless confirms an explicit individual Save or Save All item,
  `CLEANUP_INBOX_TAG` is removed and the optional completion tag is added with the linked
  uploader's credential. Refresh, Retry Review, cancel, close, recovery, and polling never mutate
  these tags.
- `CLEANUP_QUESTION_DELAY_MINUTES` and `CLEANUP_UPLOAD_DELAY_MINUTES` optionally set auto-deletion timers (0 = default daily 03:00 purge).
- `DISCORD_MAX_ATTACHMENT_BYTES` controls incoming uploads only. Outgoing files use Discord's
  effective runtime limit automatically.
- The default cleanup runs at 03:00 in `TZ`.

No additional setting is required for question search sessions or shared thread context. They use
the existing `ENCRYPTION_KEY` and `REFERENCE_CONTEXT_TTL_SECONDS` retention setting (15 minutes by
default).

Validate Compose without printing the expanded secret-bearing configuration:

```console
docker compose config --quiet
```

## 8. Build and test Paperless connectivity

Build the image:

```console
docker compose build
```

Before starting Discord, verify the configured token and internal URL from the container:

```console
docker compose run --rm --no-deps --entrypoint python paperless-assistant -c \
  'import json,os,urllib.request; base=os.environ["PAPERLESS_INTERNAL_URL"].rstrip("/"); req=urllib.request.Request(base+"/api/documents/?page_size=1",headers={"Authorization":"Token "+os.environ["PAPERLESS_TOKEN"],"Accept":"application/json; version=10"}); response=urllib.request.urlopen(req,timeout=15); json.load(response); print("Paperless API OK:",response.status)'
```

Expected output:

```text
Paperless API OK: 200
```

If this reports DNS, connection, TLS, `401`, or `403` errors, fix the internal URL, Docker network,
certificate trust, or API token before continuing. The command does not print the token.

### Optional same-project startup ordering

The standalone assistant deployment does not depend on a Compose-managed Paperless service. If
Paperless and the assistant are defined in the same Compose project, you may delay assistant
startup until Paperless is healthy:

```yaml
services:
  paperless-assistant:
    depends_on:
      paperless-webserver:
        condition: service_healthy
```

Replace `paperless-webserver` with the Paperless service name in that project. The Paperless
service must define a working `healthcheck`; a running container alone is not evidence that its API
is ready. Do not add this dependency to the standalone `compose.yaml`, where no Paperless service
is defined.

This condition controls initial Compose startup ordering only. It does not stop or restart the
assistant if Paperless becomes unhealthy later. The assistant continues to fail closed and report
dependency readiness through `/readyz`.

## 9. Start the worker

```console
docker compose up -d --wait
docker compose ps
curl --fail http://127.0.0.1:8780/healthz
curl --fail http://127.0.0.1:8780/readyz
```

Expected behavior:

- `/healthz` returns HTTP 200 when the process is alive.
- `/readyz` returns HTTP 200 after Discord connects and the exact source tag is verified.
- The Discord bot appears online.

If `curl` is unavailable, open the loopback URLs with another local HTTP client. Do not expose port
8780 through a reverse proxy or NAS firewall rule.

Review startup logs:

```console
docker compose logs --tail=100 paperless-assistant
```

The logs must not contain authorization headers or credentials. Paperless failure diagnostics are
bounded, escaped, and credential-redacted but may contain upstream validation detail, so restrict
server-log access to operators.

## 10. Manual acceptance test

Use only synthetic files and harmless test identities.

### Authorization and routing

1. Send `hello` in a DM to the bot: it should ignore the message.
2. Send `hello` in an unrelated server channel: it should ignore the message.
3. If convenient, use an account not in `DISCORD_ALLOWED_USER_IDS`: it should be ignored.
4. Send text without an attachment in `paperless-uploads`: the bot should ask for attachments.
5. Attach a file in `paperless-questions`: the bot should direct you to the uploads channel.

### Questions and retrieval

1. In `paperless-questions`, ask a natural question whose answer is present in a known synthetic
   Paperless document. Confirm the bot creates a public thread; ask a follow-up there and confirm
   the thread is reused.
2. Confirm the bot first posts `Searching Paperless…`, then edits that status with Paperless's
   native answer or safe full-text fallback results.
3. Run `/search text <question>`, `/search title <question>`, and `/search advanced <query>` in
   the thread. Confirm they use their selected Paperless search mode. Enter malformed native query
   syntax and confirm the response is fixed validation guidance, not the query or a server error.
4. Run `/search rag <question>` while native RAG is unavailable and confirm it reports that safe
   failure instead of silently changing modes.
5. Confirm a result page contains at most three cards, each with title, date, ID, **Open in
   Paperless**, **Send File**, and **Similar**. For a query with more than three results, confirm
   **Prev** and **Next** edit those cards in place and only the requester can use them.
6. Select **Open in Paperless** and verify the HTTPS browser link.
7. Select **Send File**:
   - a small original should arrive as an attachment;
   - a large original should use an archived PDF when one fits, or return the authenticated
     original-download link.
8. Select **Similar** and confirm the same thread receives no more than three related documents
   visible to your linked Paperless account. Confirm the source Paperless ID is identified and is
   not repeated in the results.
9. Ask `send me the second one` and `send me all of them`.
10. Reply directly to a result card with a follow-up question. Confirm Paperless answers about
    that document and uses the current page's result.
11. Have another allowlisted household member ask in the same thread; confirm the conversation is
    shared but uses their linked Paperless token. Run `/search reset` as either allowlisted member
    and confirm later questions start fresh while existing result controls still work for their
    requester.

Paperless controls answer quality. The assistant does not add another AI or try to make search
results exhaustive.

### Native and multi-file ingestion

1. Create a harmless UTF-8 text file and a one-page PDF containing synthetic data.
2. Attach both in one message in `paperless-uploads`.
3. Use a caption containing:
   - an exact existing tag, correspondent, or document-type name;
   - some unmatched guidance prose.
4. Confirm one immediate batch summary tracks both files independently and one bot-authored rich
   parent plus public review thread appears for each attachment.
5. In Paperless, verify:
   - both documents were submitted;
   - the exact `Discord` source tag is present;
   - unambiguous exact taxonomy names were applied;
   - Paperless chose the title and performed its normal classification;
   - each successful document has one note beginning `Discord upload guidance:`.
6. For each successful document, confirm its rich parent shows the attachment number, safe
   filename, Paperless ID/link, and bounded current and pending metadata. Inside its thread,
   confirm Discord shows:
   - a title message with **Edit Title**;
   - one **Editable Metadata** message whose closed menus visibly begin with Date,
     Correspondent, Document Type, Storage Path, and Tags;
   - one action message with **Apply Changes** and **Reset Changes**;
   - persistent per-file **Open Paperless**, **Refresh**, and **Finish & Close** controls.
   Confirm the batch summary separately exposes **Refresh All**, **Save All**, and **Close All**.
7. Confirm Paperless-matched existing objects start selected and unmatched AI names start
   unchecked. Confirm Correspondent, Document Type, and Storage Path identify the existing value
   when kept. Uncheck a match, select a close existing choice, add a harmless custom tag, choose an
   AI date, and enter a custom date. Confirm **Reset Changes** restores the original proposal
   without writing to Paperless.
8. Before saving, ensure the exact configured `CLEANUP_INBOX_TAG` exists and is attached to the
   synthetic document. If testing `AI_REVIEW_COMPLETION_TAG`, create that tag in Paperless first,
   make it visible to the uploader, and configure its exact name; the assistant must not create it.
9. Repeat the choices and select **Apply Changes**. With the default
   `REQUIRE_NEW_METADATA_CONFIRMATION=true`, confirm selected unmatched names show the separate
   create-and-apply prompt. Verify Paperless receives only the selected fields and preserves its
   unrelated tags. Confirm the inbox tag is removed, the optional completion tag is added, and
   Discord reports success before the review becomes eligible for background cleanup.
10. Make an unapplied change and select per-file **Refresh**; confirm Discord warns before
   discarding it.
   Cancel once, then confirm the refresh and verify the review remains usable. Paperless may return
   its cached LLM proposal. Confirm the inbox/completion tags did not change.
11. Make another unapplied change and select **Finish & Close**; confirm Discord warns that local
    choices will be discarded. Cancel once. After applying or resetting, select it again and
    confirm that document's thread and rich parent are deleted while unresolved sibling reviews
    remain. Confirm closing without another Save does not mutate the finalization tags.
12. As a different allowlisted user, try an AI or thread control and confirm it is rejected. As the
    uploader, change the document in Paperless before applying another proposal and confirm the
    stale review is rejected instead of overwriting the newer edit.
13. Set one `ALLOW_EDIT_*` value to `false`, recreate the container, upload a fresh synthetic
    document, and confirm that field is absent while the others remain usable. Restore it to
    `true`.
14. Set `REQUIRE_NEW_METADATA_CONFIRMATION=false`, recreate the container, select a harmless new
    taxonomy name, and choose **Apply Changes**. Confirm there is no second prompt, the object is
    created once, and it is applied. Restore the default `true` unless this is the desired policy.
15. Exercise **Save All** and confirm one prompt saves dirty or finalization-retry reviews in
    attachment order. Confirm each successful document is finalized, the aggregate response is
    delivered before cleanup eligibility, and shared artifacts remain until the existing
    all-items-resolved rule is satisfied. Exercise **Close All** and confirm one prompt closes
    successful reviews, warns about discarded local choices, and does not dismiss failures.
16. Temporarily make a configured finalization tag missing or ambiguous, or remove the uploader's
    document tag-edit permission. Save and confirm Discord says the metadata was saved but tag
    finalization needs retry/reconciliation; the review evidence must remain. Restore the exact
    tag/permission and Save again. Do not expect an ambiguous write to be retried automatically.
17. Cause one safe validation failure. Confirm it receives a per-file failure thread and the
    original upload remains. Dismiss that failure only after closing every success; confirm the
    failed thread and parent disappear, and the original upload and batch summary are then deleted
    together.
18. Leave an upload uncertain and confirm neither **Close All**, `/clean`, nor scheduled cleanup
    removes its shared artifacts or offers failure dismissal.
19. Restart the worker while a successful review remains open. Confirm recovery edits the existing
    title, metadata, action, and control messages instead of posting a duplicate review panel. Run
    `/clean` and confirm it removes only bot-owned orphan/duplicate upload artifacts while
    preserving active and uncertain reviews. Confirm recovery rendering did not mutate document
    tags.
20. Upload the same synthetic file again in a separate Discord message. Confirm the assistant sends
    it again and, only after Paperless explicitly identifies the duplicate, the per-file failure
    and batch summary suggest checking/emptying Paperless trash or uploading a genuinely different
    file. Restart during task polling and confirm recovery uses the same wording.

Rollback a review finalization in Paperless by restoring the configured inbox tag and removing the
optional completion tag. This reverses the document taxonomy state but cannot recreate Discord
artifacts that cleanup already deleted; `/clean` remains available to retry partial Discord
cleanup failures.

### Failure and recovery

1. Rename a text file to `.pdf` and upload it. The bot should reject the signature mismatch.
2. Try a HEIC file. The bot should request JPEG or PDF.
3. Try a ZIP archive. The bot should reject it without unpacking.
4. With Office uploads still disabled, try a DOCX. The bot should reject it cleanly while PDF and
   image ingestion continue working.
5. Start processing a synthetic document and restart the worker:

   ```console
   docker compose restart paperless-assistant
   ```

   Confirm a saved Paperless task resumes without a second upload and rebuilds the corresponding
   per-file parent/thread controls rather than posting an unrelated generic notification.
6. Temporarily rename or remove the exact `Discord` tag:
   - questions and downloads should continue;
   - uploads should pause;
   - `/readyz` should report not ready;
   - the uploads channel should receive one bounded warning.
7. Restore the exact tag. Within the taxonomy refresh interval, readiness should recover and the
   recorded warning should be removed.

### Optional Office/Tika acceptance

Only after the normal tests pass and Paperless Tika/Gotenberg are known to work:

1. Set `PAPERLESS_OFFICE_UPLOADS_ENABLED=true`.
2. Recreate the worker:

   ```console
   docker compose up -d --force-recreate
   ```

3. Upload synthetic DOCX, PPTX, and XLSX files.
4. Confirm successful Paperless processing.
5. If Paperless reports a failed Office task, confirm Discord provides Tika/Gotenberg setup
   guidance and the worker remains healthy.

## 11. Troubleshooting

### The bot is offline

- Check `docker compose ps` and the last 100 log lines.
- Verify the bot token has not been reset.
- Confirm the container has outbound internet access to Discord.
- Confirm only one assistant replica is running against the SQLite volume.

### Startup reports a `/data` permission error

- Confirm the service uses the `paperless-assistant-data` named volume from `compose.yaml`, not a
  Synology host-directory mount left over from an earlier setup.
- Confirm no second `/data` mapping was added in Container Manager.
- If you deliberately use the advanced bind-mount option in step 6, verify that its local Unix
  filesystem directory is owned by UID/GID `10001:10001` with mode `0700`.

### The bot is online but ignores messages

- Enable Message Content intent on the Developer Portal's **Bot** page.
- Confirm the server, channel, and user IDs are immutable copied IDs.
- Confirm the question and upload channel IDs were not reversed.
- Check channel permission overrides for the bot role.

### Readiness is degraded

- Confirm the exact unique `Discord` tag exists.
- Test the Paperless API command from step 8.
- Confirm Discord is connected.
- Liveness intentionally stays healthy during downstream failures.

### Uploads work but successful source messages remain

- Grant **Manage Messages** in the uploads channel.
- Confirm every attachment in the batch actually succeeded or resolved as a duplicate.
- Failed, timed-out, active, and reconciliation-required batches are deliberately retained.

### AI suggestions are unavailable after upload

- In Paperless 3.0.4, verify the document detail page's AI **Suggest** action works for the same
  linked user. Classic suggestions do not prove the LLM endpoint is healthy.
- Confirm `PAPERLESS_AI_ENABLED`, backend, model, endpoint, and API key in Paperless. Remember that
  Application Configuration database values override environment values.
- Keep the assistant's `PAPERLESS_AI_SUGGESTIONS_TIMEOUT_SECONDS` above Paperless's
  `PAPERLESS_AI_LLM_REQUEST_TIMEOUT`.
- **Refresh** may return Paperless's cached LLM proposal. Paperless 3.0.4 invalidates it when
  the document changes; the assistant deliberately does not add a temporary tag or otherwise
  mutate the document merely to force another LLM request.
- Inspect the assistant server log for `paperless_request_failed`; Discord intentionally shows a
  generic error.
- Embeddings are optional for per-document suggestions. Diagnose the embedding/index service only
  when RAG grounding or collection chat is also required.

### Questions return only basic results

- Test native chat in the Paperless web interface.
- Confirm Paperless's configured LLM and embeddings are healthy.
- Basic full-text search is the intentional fallback only for implicit free-form questions when
  native RAG fails or returns no content. `/search rag` deliberately never falls back.

### A file is too large for Discord

- Incoming uploads are limited by both Discord and `DISCORD_MAX_ATTACHMENT_BYTES`.
- Outgoing delivery first tries the original, then an archived PDF, then an authenticated original
  Paperless link.
- Discord's free-tier upload limit can prevent a large incoming file from reaching the bot at all;
  upload that file through Paperless's website.

## 12. Back up and update

The `paperless-assistant-data` named volume survives image updates, container recreation, and
`docker compose down`. Do not use `docker compose down -v`, delete the volume in Container
Manager, or remove it with Docker unless you intend to discard the assistant's recovery,
idempotency, context, warning, and audit state. Documents already accepted by Paperless remain in
Paperless; staging and delivery files are transient.

Routine backup is optional for the household deployment. For a consistent SQLite copy, briefly
stop the worker:

```console
docker compose stop paperless-assistant
docker compose cp paperless-assistant:/data/assistant.sqlite3 ./assistant.sqlite3
docker compose start paperless-assistant
```

Store that file as sensitive data. It contains minimized identifiers and workflow state, though
not document contents, questions, answers, captions, filenames, or tokens.

If an existing installation already uses a host bind mount, do not switch it silently to an empty
named volume. Either migrate its `assistant.sqlite3` into the named volume during a stopped
maintenance window or retain the existing absolute path using the advanced bind-mount procedure
in step 6. Verify ownership and mode even if the directory worked with a previous image.

To update after reviewing release notes:

```console
git pull --ff-only
docker compose build --pull
docker compose up -d --wait
curl --fail http://127.0.0.1:8780/readyz
```

Never copy `.env` into an issue, pull request, backup shared with others, or support transcript.
Rotate both tokens if disclosure is suspected.
