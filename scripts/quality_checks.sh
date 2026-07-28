#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${project_dir}"

export UV_MANAGED_PYTHON=1
export UV_PYTHON=3.14.6

git diff --check
uv python install "${UV_PYTHON}"
uv lock --check
uv sync --locked --all-groups
uv run --locked pre-commit run actionlint --all-files
uv run --locked ruff check --fix . || true
uv run --locked ruff format .
uv run --locked ruff check .
uv run --locked mypy
TMPDIR=/tmp uv run --locked pytest
uv run --locked pip-audit
