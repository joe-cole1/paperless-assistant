"""ASGI application construction and process entry point."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from paperless_assistant.config import Settings
from paperless_assistant.logging_config import configure_logging
from paperless_assistant.mcp_server import create_mcp_server

logger = logging.getLogger(__name__)


def _metadata(settings: Settings) -> dict[str, Any]:
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "mcp_endpoint": "/mcp",
        "bootstrap_mode": settings.mcp_bootstrap_mode,
    }


def create_app(settings: Settings | None = None) -> Starlette:
    """Construct the HTTP and MCP application from validated settings."""
    resolved = settings or Settings()
    mcp = create_mcp_server(resolved)

    async def service_metadata(_: Request) -> JSONResponse:
        return JSONResponse(_metadata(resolved))

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "service": resolved.app_name, "version": resolved.app_version}
        )

    async def ready(request: Request) -> JSONResponse:
        is_ready = bool(request.app.state.ready)
        return JSONResponse(
            {
                "status": "ok" if is_ready else "not_ready",
                "service": resolved.app_name,
                "version": resolved.app_version,
            },
            status_code=200 if is_ready else 503,
        )

    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncIterator[None]:
        configure_logging(resolved.log_level)
        logger.info(
            "service_starting",
            extra={
                "service": resolved.app_name,
                "version": resolved.app_version,
                "environment": resolved.app_env,
                "mcp_mode": "bootstrap" if resolved.mcp_bootstrap_mode else "standard",
            },
        )
        async with mcp.session_manager.run():
            application.state.ready = True
            logger.info("service_ready", extra={"service": resolved.app_name, "ready": True})
            try:
                yield
            finally:
                application.state.ready = False
                logger.info(
                    "service_shutdown", extra={"service": resolved.app_name, "ready": False}
                )

    application = Starlette(
        debug=False,
        routes=[
            Route("/", service_metadata, methods=["GET"]),
            Route("/healthz", health, methods=["GET"]),
            Route("/readyz", ready, methods=["GET"]),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )
    application.state.ready = False
    return application


app = create_app()


def main() -> None:
    """Run the ASGI service with Uvicorn using validated environment settings."""
    settings = Settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=True,
    )


if __name__ == "__main__":
    main()
