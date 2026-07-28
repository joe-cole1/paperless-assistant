#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="${project_dir}/tests/paperless_ai/compose.yaml"
project_name="paperless-ai-it-${BASHPID}"
export PAPERLESS_AI_TEST_PORT="${PAPERLESS_AI_TEST_PORT:-18765}"

cleanup() {
  docker compose --project-name "${project_name}" --file "${compose_file}" down --volumes \
    --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose --project-name "${project_name}" --file "${compose_file}" up --detach \
  --quiet-pull

PAPERLESS_AI_TEST_PORT="${PAPERLESS_AI_TEST_PORT}" \
  uv run --locked python tests/paperless_ai/run.py
