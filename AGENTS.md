# Agent instructions

These instructions apply to the entire repository.

## Start with the issue

- Read the relevant GitHub issue before changing files. Run `gh issue view <number>` when an issue number is supplied.
- GitHub issues are the canonical source for features, bugs, refactors, and deployment work.
- Restate the goal, acceptance criteria, dependencies, risks, and implementation plan before editing.
- Work on `issue/<number>-<short-description>`, never directly on the default branch unless explicitly instructed.
- Keep one primary issue per branch. Do not invent an issue number or silently expand scope.
- Record newly discovered work as a proposed follow-on issue rather than folding it into the current change.

## Understand the design

- Read `architecture.md` before architectural or trust-boundary changes.
- Read the applicable records in `docs/adr/`; add or supersede an ADR for consequential decisions.
- Preserve the modular-monolith, ports-and-adapters boundaries. Inbound adapters call application services, not other inbound adapters.
- Implement only currently required capabilities. Do not add speculative Paperless, Gemini, or Discord integrations.

## Security and privacy

- Preserve read-only, fail-closed, and least-privilege defaults.
- Treat credentials, tokens, authorization headers, private document content, OCR text, document binaries, personal data, Discord identifiers, and LLM inputs as sensitive.
- Never expose sensitive data in issues, logs, commits, fixtures, screenshots, or pull requests.
- Never commit `.env` or credentials. Use sanitized examples and synthetic test data.
- Do not enable document access before production MCP authentication exists.
- Write capabilities require a dedicated issue, security review, separate credentials and scopes, confirmation, idempotency, audit records, and a rollback strategy.

## Quality and delivery

- Keep changes small and coherent; use full type annotations and async I/O at network boundaries.
- Update tests, documentation, configuration examples, and ADRs with the behavior they describe.
- Run the relevant Ruff lint/format checks, mypy, pytest with coverage, dependency audit, and container checks.
- Never bypass a failing check or fabricate successful test output. Report unrun or failing checks plainly.
- Review `git status`, configured remotes, and the final Git diff before committing. Check for credentials and generated artifacts.
- Use small, meaningful commits when practical. Never force-push, rewrite shared history, or discard user changes.
- Pull requests must link the issue and contain `Closes #<number>` or `Refs #<number>` as appropriate.
- Before pushing more changes to a branch with an open pull request, inspect its checks and review state.

## Repository operations

- Use GitHub MCP for repository, issue, and pull-request context when available; use `gh` and local Git for local operations.
- Keep GitHub Actions pinned to immutable commit SHAs and grant minimal permissions.
- Never place production secrets in CI. Keep containers non-root and hardened unless an issue documents and reviews an exception.
- Follow private vulnerability reporting guidance in `SECURITY.md`.
