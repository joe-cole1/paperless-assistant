# Paperless Assistant

Paperless Assistant is a self-hosted document-assistant platform for a family Paperless-ngx deployment. Its long-term design shares secure search, retrieval, question answering, delivery, ingestion, and controlled metadata services across Gemini Spark, Discord, and other front ends.

The repository currently implements **Phase 0 only**: a small MCP connectivity probe with service/health endpoints and one harmless `ping` tool.

> **Security warning:** The bootstrap endpoint is unauthenticated and exposes only `ping`. Do not enable Paperless tools until MCP authentication is implemented. The container has no Paperless token, Gemini credential, Discord credential, document access, upload tool, or write tool.

## Architecture

The project uses a ports-and-adapters modular monolith. MCP and future Discord processes are inbound adapters that call shared application services. Paperless, LLM, audit, and file-delivery integrations are outbound adapters. Phase 0 intentionally implements only the configuration, HTTP/MCP adapter, and connectivity tool.

See [architecture.md](architecture.md) for trust boundaries, future interfaces, Discord design, authentication, privacy controls, and the phased roadmap. Accepted decisions are in [docs/adr](docs/adr).

## Prerequisites

- Git
- [uv 0.11.31 or compatible stable release](https://docs.astral.sh/uv/)
- Python 3.14 (uv can install it)
- Docker Engine with Docker Compose v2 for container workflows
- Node.js only if using MCP Inspector

## Local development

```console
uv python install 3.14
uv sync --locked
cp .env.example .env
uv run paperless-assistant
```

The direct development server uses the `HOST` and `PORT` values in `.env`; the example binds port 8000. To retain the loopback-only Python default instead, run without `.env` or set `HOST=127.0.0.1`.

Common commands:

```console
# Verify/sync the dependency lock
uv lock --check
uv sync --locked

# Run all tests and enforce coverage
uv run --locked pytest

# Run one test
uv run --locked pytest --no-cov tests/unit/test_config.py::test_safe_defaults

# Lint, automatically fix safe lint findings, and format
uv run --locked ruff check .
uv run --locked ruff check . --fix
uv run --locked ruff format .
uv run --locked ruff format --check .

# Strict type checking and dependency audit
uv run --locked mypy
uv run --locked pip-audit

# Run all local pre-commit checks
uv run --locked pre-commit run --all-files
```

Configuration is typed and fails startup on invalid values. `MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` accept comma-separated values. DNS-rebinding protection is always enabled. CORS is not enabled. Never commit `.env`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Non-secret service name, version, environment, MCP path, and bootstrap mode |
| `GET` | `/healthz` | Process liveness JSON; returns 200 while the process can serve HTTP |
| `GET` | `/readyz` | Lifecycle readiness JSON; returns 200 after MCP startup |
| `POST` | `/mcp` | Official MCP Streamable HTTP transport |

The MCP catalog contains only `ping`. Unknown HTTP routes return 404 responses.

## MCP Inspector

Start the service, then launch the official Inspector:

```console
npx @modelcontextprotocol/inspector
```

In the Inspector UI, choose **Streamable HTTP**, enter `http://127.0.0.1:8000/mcp` (or port 8780 for Compose), connect, list tools, and call `ping`. No authentication is configured during bootstrap.

## Docker Compose

```console
cp .env.example .env
docker compose build
docker compose up -d --wait
docker compose ps
docker compose logs -f paperless-mcp
python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8780/healthz')))"
uv run --locked python scripts/mcp_smoke_test.py --url http://127.0.0.1:8780/mcp
docker compose down
```

The container listens on 8000 and Compose publishes host port 8780. It runs as UID 10001 with a read-only root filesystem, small temporary filesystem, no Linux capabilities, and `no-new-privileges`.

The complete automated container check builds, starts, waits for health, inspects `/healthz`, calls `ping` through the official MCP client, confirms non-root execution, and always stops the stack:

```console
./scripts/container_smoke_test.sh
```

It exits non-zero on failure.

## Pangolin target

The future production mapping is:

- External: `https://paperless-mcp.thecolefam.com/mcp`
- Target: `http://<NAS-IP>:8780/mcp`
- Health check: `GET /healthz`
- Expected: HTTP 200

Before enabling the mapping, append `paperless-mcp.thecolefam.com` to `MCP_ALLOWED_HOSTS`. Add an allowed HTTPS Origin only if a real browser client sends that Origin; do not use a wildcard. Pangolin terminates TLS, but its normal interactive browser login is not a substitute for MCP OAuth.

## Gemini Spark connectivity probe

After the Pangolin route is healthy, add a custom connected app in Gemini Spark using `https://paperless-mcp.thecolefam.com/mcp` as its remote MCP URL. For Phase 0 only, select the no-auth option when the product offers it, connect, discover the single `ping` tool, and invoke it. Remove or disable the connection if any tool beyond `ping` appears.

Do not add Paperless credentials or tools to this unauthenticated deployment. The next phase must implement and validate OAuth 2.1-compatible MCP authorization (with Dynamic Client Registration where practical, or a reviewed manual bearer configuration supported by Spark) before document access.

## Roadmap

1. Phase 0: MCP ping, health checks, Docker, CI — implemented here.
2. Phase 1: production MCP authentication, Spark test, security validation.
3. Phase 2: dedicated read-only Paperless client, search/retrieval, source attribution, bounded OCR.
4. Phase 3: read-only Discord worker, immutable user allowlist, search/Q&A, supported LLM API.
5. Phase 4: authorized document delivery and short-lived links.
6. Phase 5: confirmed Discord ingestion and Paperless task tracking.
7. Phase 6: separately credentialed/scoped controlled metadata writes with dry runs and audits.
8. Phase 7: optional multi-user authorization, durable jobs/audit storage, and dashboards.

Future phases are designs, not current capabilities.

## Contributing and security

Every feature, bug, refactor, or deployment change begins with a GitHub issue. Use `issue/<number>-<short-description>`, keep one primary issue per branch, run all quality checks, update documentation with behavior, and link the pull request using `Closes #<number>` or `Refs #<number>`. See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

Report vulnerabilities privately according to [SECURITY.md](SECURITY.md). Never put credentials or private document content in an issue, log, test fixture, commit, or pull request.

## License

MIT; see [LICENSE](LICENSE).
