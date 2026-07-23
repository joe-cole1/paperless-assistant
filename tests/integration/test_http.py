"""HTTP integration tests for service and probe endpoints."""

from __future__ import annotations

import httpx
import pytest

from paperless_assistant.app import create_app
from paperless_assistant.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.mark.asyncio
async def test_ready_is_unavailable_before_startup(settings: Settings) -> None:
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    assert response.json()["status"] == "not_ready"


@pytest.mark.asyncio
async def test_http_endpoints_and_unknown_route(settings: Settings) -> None:
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)

    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(transport=transport, base_url="http://localhost") as client,
    ):
        metadata = await client.get("/")
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
        missing = await client.get("/does-not-exist")

    assert metadata.status_code == 200
    assert metadata.headers["content-type"] == "application/json"
    assert metadata.json() == {
        "service": "paperless-assistant",
        "version": "0.1.0",
        "environment": "development",
        "mcp_endpoint": "/mcp",
        "bootstrap_mode": True,
    }
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "paperless-assistant",
        "version": "0.1.0",
    }
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("text/plain")
    assert missing.text == "Not Found"

    combined = " ".join([metadata.text, health.text, ready.text, missing.text]).lower()
    for forbidden in ("token", "credential", "authorization", "document content"):
        assert forbidden not in combined


@pytest.mark.asyncio
async def test_mcp_dns_rebinding_protection_rejects_host_and_origin(settings: Settings) -> None:
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    trusted_transport = httpx.ASGITransport(app=application)

    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(transport=transport, base_url="http://untrusted.example") as client,
        httpx.AsyncClient(
            transport=trusted_transport, base_url="http://localhost"
        ) as trusted_client,
    ):
        invalid_host = await client.post("/mcp", json={})
        invalid_origin = await trusted_client.post(
            "/mcp", json={}, headers={"origin": "https://untrusted.example"}
        )

    assert invalid_host.status_code == 421
    assert invalid_host.text == "Invalid Host header"
    assert invalid_origin.status_code == 403
    assert invalid_origin.text == "Invalid Origin header"
