# Contributing

Paperless Assistant uses issue-first development so security, privacy, scope, and acceptance criteria are reviewed before code changes.

## Workflow

1. Search existing GitHub issues. Create an issue from the feature or bug template when no suitable issue exists.
2. Agree on the issue's scope, non-goals, acceptance criteria, security impact, tests, deployment, and rollback considerations.
3. Read `architecture.md` and applicable records in `docs/adr/`.
4. Create `issue/<number>-<short-description>` from the current default branch.
5. Keep one primary issue per branch. Propose unrelated work as a follow-on issue.
6. Implement code, tests, documentation, configuration examples, and architecture records together.
7. Run the required checks below and review the final diff for sensitive or generated content.
8. Open a pull request containing `Closes #<number>` (or `Refs #<number>` when it must remain open).

Never commit directly to the default branch unless a repository owner explicitly instructs you to do so. Do not force-push shared branches or bypass failing checks.

## Development setup

Install [uv](https://docs.astral.sh/uv/) and Python 3.14, then run:

```console
uv python install 3.14
uv sync --locked
uv run pre-commit install
```

## Required checks

```console
./scripts/ship_check.sh
```

The ship gate is the same quality and container suite used by `.github/workflows/ci.yml`. It
checks the lock, GitHub Actions syntax, Ruff lint and formatting, mypy, pytest and coverage,
dependency vulnerabilities, and the hardened container runtime. Run it immediately before every
push.

Do not claim a command passed unless you ran it and observed the result. If a check cannot run locally, say so in the pull request and rely on a clearly identified CI result.

## Security and data handling

Use only synthetic test data. Never post tokens, authorization headers, private document text or binaries, personal data, Discord credentials, or Gemini credentials in public or private GitHub artifacts. Report vulnerabilities using `SECURITY.md` rather than a normal issue.

Read-only and least-privilege defaults are architectural constraints. Any write operation needs a dedicated issue and security review.
