#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${project_dir}"

./scripts/quality_checks.sh
./scripts/container_smoke_test.sh
./scripts/paperless_ai_integration_test.sh
