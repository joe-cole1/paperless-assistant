# ADR 0001: Python and uv

- Status: Accepted
- Date: 2026-07-23

## Context

The service needs a well-supported async ecosystem, an official MCP SDK, strict typing, reproducible dependencies, fast local workflows, and a small Linux container suitable for the Synology DS920+.

## Decision

Use CPython 3.14 and manage project environments and the committed lock with uv. Production installs use `uv sync --locked --no-dev`; CI verifies that the lock is current before syncing. Project, test, lint, type-check, and coverage configuration lives in `pyproject.toml` where practical.

## Consequences

Developers and CI use one lock-based workflow. The Docker build can install exactly the resolved runtime set. Python and uv upgrades are explicit dependency work and require compatibility checks.

## Alternatives considered

- Python 3.12 or 3.13: supported, but 3.14 is current, supported by the selected dependencies, and available as an official image.
- pip plus requirements files: workable but splits project metadata and locking workflows.
- Poetry or PDM: capable, but uv provides the required lock and Python/project management with fewer moving parts.
