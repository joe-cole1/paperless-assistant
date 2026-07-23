# Security policy

## Reporting a vulnerability

Do not open a public GitHub issue for a suspected vulnerability. Use GitHub's private vulnerability reporting for this repository when available. If that channel is unavailable, contact the repository owner privately and share only the minimum information needed to establish a secure reporting channel.

Do not include live credentials, tokens, authorization headers, private document content, OCR text, document binaries, personal data, Discord bot tokens, or Gemini API keys in reports. Revoke any credential that may have been disclosed.

## Sensitive data

Paperless documents, metadata, OCR text, search excerpts, account identifiers, audit attribution, temporary files, delivery links, API credentials, and LLM prompts or responses derived from documents are sensitive. Logs and tests must use sanitized metadata and synthetic content.

## Current security posture

Phase 0 is an unauthenticated connectivity probe. It exposes service metadata, health/readiness probes, and a single harmless `ping` MCP tool. It has no Paperless token, document access, Gemini credentials, Discord credentials, uploads, or write operations.

Do not enable Paperless tools until OAuth-compatible MCP authentication and in-application token validation are implemented and reviewed. The initial Paperless capability must use a dedicated read-only service account with bounded retrieval.

## Future consequential capabilities

Paperless ingestion and metadata writes are disabled by default. Enabling them requires a dedicated issue, threat/security review, separate credentials and scopes, explicit authorization and confirmation, bounded targets, idempotency protection, before/after audit records, tests, deployment and rollback plans. Destructive document operations are out of scope unless separately approved after a focused review.

Supported security updates currently track the default branch until formal releases begin.
