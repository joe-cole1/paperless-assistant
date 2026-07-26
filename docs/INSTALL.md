# Install and test Paperless Assistant

This guide walks through a private household installation from an existing Paperless-ngx v3
server to a working Discord bot. It assumes Docker Engine, Docker Compose v2, Git, and a private
Discord server.

The assistant does not expose a Discord webhook or public application endpoint. It connects
outbound to Discord, calls Paperless through its API, and binds health monitoring only to
`127.0.0.1`.

## 1. Check Paperless first

Before creating the bot:

1. Sign in to Paperless as the administrator whose token the trusted MVP will use.
2. Confirm native document chat works in the Paperless web interface. Ask a question that should
   find a known synthetic or non-sensitive test document.
3. Create exactly one Paperless tag named `Discord`.
   - The name is configurable, but it must be unique when compared case-insensitively.
   - Do not create both `Discord` and `discord`.
4. Open the user menu, select **My Profile**, and generate or regenerate the API token with the
   circular-arrow control.
5. Put the token directly into a password manager. Do not paste it into Discord, an issue, a shell
   command, or a chat transcript.
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
2. Open **OAuth2 → URL Generator** and select only the `bot` scope.
3. Grant:
   - View Channels
   - Send Messages
   - Read Message History
   - Attach Files
   - Embed Links
   - Manage Messages
4. Copy and open the generated URL. If the Portal does not show a generated URL, replace
   `APPLICATION_ID` in this equivalent authorization URL:

   ```text
   https://discord.com/oauth2/authorize?client_id=APPLICATION_ID&integration_type=0&scope=bot&permissions=125952
   ```

5. Choose **Add to server** and select the private household server.

Discord requires the installing account to have permission to manage the server. The bot does not
need Administrator.

After installation, review the two private channels' permission overrides. The bot role must have
the permissions above in both channels. **Manage Messages** lets it remove successfully processed
upload messages and retained status messages. Do not grant it access to unrelated private channels.

Discord explains bot tokens, scopes, and least-privilege server permissions in its
[OAuth2 and permissions guide](https://docs.discord.com/developers/platform/oauth2-and-permissions).

## 5. Copy immutable Discord IDs

In Discord:

1. Open **User Settings → Advanced** and enable **Developer Mode**.
2. Right-click the server and select **Copy Server ID**.
3. Right-click `paperless-questions` and copy its channel ID.
4. Right-click `paperless-uploads` and copy its channel ID.
5. Right-click each allowed household member and copy the user ID.

Use IDs, not display names. The allowlist is a JSON array such as:

```dotenv
DISCORD_ALLOWED_USER_IDS=[111111111111111111,222222222222222222]
```

## 6. Install the assistant

Choose a private directory on the Docker host:

```console
git clone https://github.com/joe-cole1/paperless-assistant.git
cd paperless-assistant
cp .env.example .env
chmod 600 .env
mkdir -p ./data
chmod 700 ./data
sudo chown -R 10001:10001 ./data
```

If testing a not-yet-merged branch, check out that branch before copying `.env`:

```console
git switch issue/10-discord-paperless-assistant
```

On Synology, use a durable absolute host directory instead of `./data`, create it first, and make
UID/GID `10001:10001` its owner. Do not place the state directory in a temporary package or
download directory.

## 7. Configure `.env`

Edit `.env` locally on the Docker host. At minimum, replace:

```dotenv
TZ=America/New_York

DISCORD_TOKEN=replace-with-discord-bot-token
DISCORD_GUILD_ID=123456789012345678
DISCORD_QUESTIONS_CHANNEL_ID=123456789012345679
DISCORD_UPLOADS_CHANNEL_ID=123456789012345680
DISCORD_ALLOWED_USER_IDS=[123456789012345681,123456789012345682]

PAPERLESS_INTERNAL_URL=http://paperless-webserver:8000
PAPERLESS_PUBLIC_URL=https://paperless.example.invalid
PAPERLESS_TOKEN=replace-with-paperless-api-token
PAPERLESS_SOURCE_TAG=Discord
PAPERLESS_OFFICE_UPLOADS_ENABLED=false

PAPERLESS_ASSISTANT_DATA_PATH=./data
```

Important details:

- `PAPERLESS_INTERNAL_URL` must work from inside this container, not merely from the Docker host.
- `PAPERLESS_PUBLIC_URL` must be the HTTPS browser URL your wife can open.
- Do not add `Token ` before `PAPERLESS_TOKEN`; supply only the token value.
- Keep Office uploads `false` for the first startup.
- `DISCORD_MAX_ATTACHMENT_BYTES` controls incoming uploads only. Outgoing files use Discord's
  effective runtime limit automatically.
- The default cleanup runs at 03:00 in `TZ`.
- The MVP intentionally uses the administrator's Paperless token. Issue #8 tracks future
  least-privilege identities.

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

The logs must not contain tokens, questions, answers, filenames, captions, document titles, OCR, or
taxonomy values.

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
   Paperless document.
2. Confirm the bot first posts `Searching Paperless…`, then edits that status with Paperless's
   native answer.
3. Confirm it shows no more than three referenced documents.
4. Confirm each result shows title, date, ID, **Open in Paperless**, and **Send File**.
5. Select **Open in Paperless** and verify the HTTPS browser link.
6. Select **Send File**:
   - a small original should arrive as an attachment;
   - a large original should use an archived PDF when one fits, or return the authenticated
     original-download link.
7. Ask `send me the second one` and `send me all of them`.
8. Reply directly to a result message with a follow-up question. Confirm Paperless answers about
   that document.
9. With several prior results, ask an ambiguous follow-up such as `what about the date?`; the bot
   should ask which result you mean.

Paperless native chat controls answer quality and returns at most three references. The assistant
does not add another AI or try to make the result exhaustive.

### Native and multi-file ingestion

1. Create a harmless UTF-8 text file and a one-page PDF containing synthetic data.
2. Attach both in one message in `paperless-uploads`.
3. Use a caption containing:
   - an exact existing tag, correspondent, or document-type name;
   - some unmatched guidance prose.
4. Confirm one batch status tracks both files independently.
5. In Paperless, verify:
   - both documents were submitted;
   - the exact `Discord` source tag is present;
   - unambiguous exact taxonomy names were applied;
   - Paperless chose the title and performed its normal classification;
   - each successful document has one note beginning `Discord upload guidance:`.
6. Confirm the original Discord upload message is removed only after every file succeeds or
   resolves as a Paperless duplicate.
7. Upload the same synthetic file again in a separate Discord message. Confirm the assistant sends
   it again and Paperless owns duplicate handling.

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

   Confirm a saved Paperless task resumes without a second upload.
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

### Questions return only basic results

- Test native chat in the Paperless web interface.
- Confirm Paperless's Gemini-backed AI configuration and embeddings are healthy.
- Basic full-text search is the intentional fallback when native chat fails or returns no content.

### A file is too large for Discord

- Incoming uploads are limited by both Discord and `DISCORD_MAX_ATTACHMENT_BYTES`.
- Outgoing delivery first tries the original, then an archived PDF, then an authenticated original
  Paperless link.
- Discord's free-tier upload limit can prevent a large incoming file from reaching the bot at all;
  upload that file through Paperless's website.

## 12. Back up and update

Back up the configured host data directory, especially `assistant.sqlite3`. Staging and delivery
files are transient.

To update after reviewing release notes:

```console
git pull --ff-only
docker compose build --pull
docker compose up -d --wait
curl --fail http://127.0.0.1:8780/readyz
```

Never copy `.env` into an issue, pull request, backup shared with others, or support transcript.
Rotate both tokens if disclosure is suspected.
