# Paperless Assistant

Paperless Assistant is a security-focused, self-hosted Discord interface for a family
Paperless-ngx deployment.

The repository is currently between runtime phases. The cancelled Gemini Spark/MCP transport has
been retired. The implemented process exposes only private health endpoints while the outbound
Discord worker is introduced in issue #10.

## Architecture

The project is a ports-and-adapters modular monolith. Discord is the inbound adapter. Application
services enforce policy, and Paperless-ngx, persistence, audit, and file delivery are outbound
adapters. No inbound adapter calls another adapter.

See [architecture.md](architecture.md) and the accepted records in [docs/adr](docs/adr).

## Prerequisites

- Git
- uv 0.11.31 or a compatible stable release
- Python 3.14
- Docker Engine with Docker Compose v2

## Local development

```console
uv python install 3.14
uv sync --locked
cp .env.example .env
uv run paperless-assistant
```

The safe Python default binds health endpoints to `127.0.0.1:8000`. The Compose example binds the
container listener to NAS loopback at `127.0.0.1:8780`.

Common checks:

```console
uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv run --locked pip-audit
docker compose build
./scripts/container_smoke_test.sh
```

## Private endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Non-secret service metadata |
| `GET` | `/healthz` | Process liveness |
| `GET` | `/readyz` | Application lifecycle readiness |

MCP and OAuth routes do not exist. Unknown routes return 404.

## Docker Compose

```console
cp .env.example .env
docker compose build
docker compose up -d --wait
docker compose ps
docker compose logs -f paperless-assistant
python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8780/healthz')))"
docker compose down
```

The container runs as UID 10001 with a read-only root filesystem, bounded `/tmp`, no Linux
capabilities, and `no-new-privileges`. The host mapping is loopback-only and must not be routed
through a public reverse proxy.

The smoke test builds and starts the stack, checks liveness/readiness, verifies non-root and
read-only execution, and always tears the stack down.

## Roadmap

1. MCP retirement and private health foundation — implemented by issue #9.
2. Discord-native Paperless chat, delivery, and immediate multi-file ingestion — issue #10.
3. Least-privilege Paperless service identities and document permissions — issue #8.

## Security

Never commit `.env`, Discord tokens, Paperless tokens, private document content, OCR, captions,
questions, answers, or personal identifiers. Report vulnerabilities privately according to
[SECURITY.md](SECURITY.md).

Every implementation begins with a GitHub issue and uses an `issue/<number>-<description>` branch.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT; see [LICENSE](LICENSE).
