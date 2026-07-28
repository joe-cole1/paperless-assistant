#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${project_dir}"

created_env=0
if [[ ! -f .env ]]; then
  cp .env.example .env
  smoke_encryption_key="$(
    python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
  )"
  sed -i "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=${smoke_encryption_key}|" .env
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
docker compose run \
  --detach \
  --rm \
  --service-ports \
  --name PaperlessAssistant \
  --entrypoint python \
  paperless-assistant \
  -c "import uvicorn; from paperless_assistant.app import create_app; uvicorn.run(create_app(start_worker=False), host='0.0.0.0', port=8000, log_config=None)"

python3 - <<'PY'
import json
import time
from urllib.request import urlopen

for attempt in range(30):
    try:
        with urlopen("http://127.0.0.1:8780/healthz", timeout=2) as response:
            payload = json.load(response)
        break
    except OSError:
        if attempt == 29:
            raise
        time.sleep(1)
if response.status != 200 or payload.get("status") != "ok":
    raise SystemExit(f"unhealthy response: {response.status} {payload}")
with urlopen("http://127.0.0.1:8780/readyz", timeout=5) as response:
    payload = json.load(response)
if response.status != 200 or payload.get("status") != "ok":
    raise SystemExit(f"unready response: {response.status} {payload}")
print("Private HTTP health checks succeeded.")
PY

runtime_uid="$(docker compose exec -T paperless-assistant id -u)"
if [[ "${runtime_uid}" -eq 0 ]]; then
  echo "Container unexpectedly runs as root." >&2
  exit 1
fi
echo "Container runs as non-root UID ${runtime_uid}."

data_permissions="$(docker compose exec -T paperless-assistant stat -c '%u:%g:%a' /data)"
if [[ "${data_permissions}" != "10001:10001:700" ]]; then
  echo "Persistent data volume has unsafe ownership or permissions: ${data_permissions}." >&2
  exit 1
fi
echo "Persistent data volume is owned by UID/GID 10001:10001 with mode 0700."

read_only="$(docker inspect PaperlessAssistant --format '{{.HostConfig.ReadonlyRootfs}}')"
if [[ "${read_only}" != "true" ]]; then
  echo "Container root filesystem is not read-only." >&2
  exit 1
fi
echo "Container root filesystem is read-only."
