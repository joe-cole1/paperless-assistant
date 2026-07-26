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
- Maintain 100% statement and branch coverage. Every bug fix must include a regression test that
  fails without the fix and passes with it; do not use exclusions or trivial assertions to hide
  untested behavior.
- Before every push, run `./scripts/ship_check.sh` unless the user explicitly instructs you to
  skip local CI for that push. Record an explicit waiver and the unrun checks plainly in the pull
  request and final report. The ship gate must stay synchronized with `.github/workflows/ci.yml`;
  it runs the lock check, actionlint, Ruff lint/format, mypy, pytest with coverage, dependency
  audit, and container smoke test using the same managed Python version and Linux
  temporary-filesystem semantics as CI.
- Do not substitute partial or approximate commands for the ship gate. If it cannot run or fails,
  do not push; report the exact failure and resolve it first.
- Never bypass a failing check or fabricate successful test output. Report unrun or failing checks plainly.
- Review `git status`, configured remotes, and the final Git diff before committing. Check for credentials and generated artifacts.
- Use small, meaningful commits when practical. Never force-push, rewrite shared history, or discard user changes.
- Pull requests must link the issue and contain `Closes #<number>` or `Refs #<number>` as appropriate.
- Every pull request must have at least one label before merge. Prefer a category label from
  `.github/release.yml` so generated release notes classify the change correctly.
- Before pushing more changes to a branch with an open pull request, inspect its checks and review state.

## Repository operations

- Use GitHub MCP for repository, issue, and pull-request context when available; use `gh` and local Git for local operations.
- A sandboxed `gh auth status` can falsely report an invalid token. Before reporting an
  authentication blocker or asking the user to refresh credentials, retry the check or the
  required GitHub operation with unrestricted network access.
- Keep GitHub Actions pinned to immutable commit SHAs and grant minimal permissions.
- Never place production secrets in CI. Keep containers non-root and hardened unless an issue documents and reviews an exception.
- Follow private vulnerability reporting guidance in `SECURITY.md`.
