# Security policy

## Reporting a vulnerability

Do not open a public GitHub issue for a suspected vulnerability. Use GitHub private vulnerability
reporting when available. Otherwise contact the repository owner privately and disclose only what
is necessary to establish a secure reporting channel.

Do not include credentials, authorization headers, private document content, OCR, files, personal
data, Discord identifiers, questions, answers, captions, notes, or provider prompts in reports.
Revoke any credential that may have been disclosed.

## Sensitive data

Paperless documents and metadata, Discord messages and attachments, account identifiers, audit
attribution, staged files, delivery links, Paperless tokens, Discord tokens, and AI-derived
content are sensitive. Logs, tests, screenshots, issues, commits, and pull requests use synthetic
data and sanitized metadata only.

## Current security posture

The implemented runtime exposes only non-secret metadata and liveness/readiness endpoints bound
to private container/NAS loopback interfaces. MCP, OAuth/JWT/JWKS, Gemini Spark, Pangolin, and
their public routes have been retired. The current foundation has no Discord or Paperless
credential and no document capability.

The container runs as a non-root user with a read-only root filesystem, dropped Linux
capabilities, `no-new-privileges`, and bounded temporary storage.

## Discord-first capability boundary

Issue #10 introduces document read, download, upload, and note operations through one private
outbound Discord worker. Exact guild, channel, and immutable user-ID allowlists are mandatory.
Application code contains only explicitly reviewed Paperless operations and never exposes a
general API proxy or administrative operation.

The initial trusted deployment uses an operator-supplied Paperless admin token. This is a known
transitional risk. Issue #8 tracks separate least-privilege service identities, object ownership,
groups, and document permissions before additional users or interfaces are added.

Destructive document operations, taxonomy creation, unrestricted metadata mutation, public share
links, bot-hosted public file endpoints, and additional inbound interfaces remain prohibited
without a dedicated issue and security review.

Supported security updates track the default branch until formal releases begin.
