## Linked issue

Closes #

## Summary

<!-- What changed and why? -->

## Scope

<!-- What is included, and what is deliberately excluded? -->

## Tests run

<!-- Paste exact commands and honest results. Never include secrets or private data. -->

## Documentation changed

<!-- README, architecture.md, ADRs, configuration examples, operations docs -->

## Security and privacy review

- [ ] Read-only and least-privilege defaults are preserved or a reviewed exception is documented.
- [ ] No credentials, authorization headers, private document content, personal data, or unsafe logs are included.
- [ ] Authentication, authorization, rate limits, and abuse cases were considered where applicable.

## Deployment notes

<!-- Configuration, proxy, resource, migration, and compatibility impact -->

## Rollback notes

<!-- How can this be disabled or reverted safely? -->

## Checklist

- [ ] I read the issue, `architecture.md`, and applicable ADRs.
- [ ] The branch has one primary issue and does not silently expand scope.
- [ ] Tests and documentation changed with behavior.
- [ ] Ruff lint and format checks pass.
- [ ] mypy passes.
- [ ] pytest passes at the required coverage threshold.
- [ ] Dependency and container checks pass or exceptions are documented.
- [ ] I reviewed the final diff and checked for secrets and generated artifacts.
- [ ] This PR contains `Closes #…` or `Refs #…`.
- [ ] This PR has at least one label, preferably a release-note category from `.github/release.yml`.
