#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${project_dir}"

created_env=0
if [[ ! -f .env ]]; then
  cp .env.example .env
  created_env=1
fi

cleanup() {
  docker compose down --remove-orphans
  if [[ "${created_env}" -eq 1 ]]; then
    rm -f .env
  fi
}
trap cleanup EXIT

docker compose build --pull
docker compose up -d --wait --wait-timeout 90

uv run --locked python - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8780/healthz", timeout=5) as response:
    payload = json.load(response)
if response.status != 200 or payload.get("status") != "ok":
    raise SystemExit(f"unhealthy response: {response.status} {payload}")
print("HTTP health check succeeded.")
PY

uv run --locked python scripts/mcp_smoke_test.py --url http://127.0.0.1:8780/mcp

runtime_uid="$(docker compose exec -T paperless-mcp id -u)"
if [[ "${runtime_uid}" -eq 0 ]]; then
  echo "Container unexpectedly runs as root." >&2
  exit 1
fi
echo "Container runs as non-root UID ${runtime_uid}."
