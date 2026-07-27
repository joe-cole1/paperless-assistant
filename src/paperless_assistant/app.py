"""Discord worker composition root and private health application."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from paperless_assistant.config import Settings
from paperless_assistant.discord_adapter import DiscordAssistant
from paperless_assistant.logging_config import configure_logging
from paperless_assistant.paperless import HttpPaperlessGateway
from paperless_assistant.repository import SQLiteRepository
from paperless_assistant.services import (
    DeliveryService,
    IngestionService,
    QueryService,
    TaxonomyCache,
)

logger = logging.getLogger(__name__)


class Runtime:
    """Own all long-lived adapters and background tasks."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.instance_id = uuid4()
        self.repository = SQLiteRepository(
            settings.database_path,
            lease_seconds=settings.instance_lease_seconds,
        )
        self.paperless = HttpPaperlessGateway(settings)
        self.taxonomy = TaxonomyCache(settings, self.paperless)
        self.query = QueryService(settings, self.paperless, self.repository, self.repository)
        self.ingestion = IngestionService(
            settings,
            self.paperless,
            self.repository,
            self.repository,
            self.taxonomy,
        )
        self.delivery = DeliveryService(settings, self.paperless, self.repository)
        self.discord = DiscordAssistant(
            settings,
            self.query,
            self.ingestion,
            self.delivery,
            self.taxonomy,
            ready_callback=self._set_ready,
        )
        self.ready = False
        self._tasks: set[asyncio.Task[None]] = set()

    def _set_ready(self, value: bool) -> None:
        self.ready = value

    async def start(self) -> None:
        """Initialize private state, acquire the singleton, and start Gateway work."""
        for directory in (
            self.settings.data_dir,
            self.settings.staging_dir,
            self.settings.delivery_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        await self.repository.initialize()
        if not await self.repository.acquire_instance(self.instance_id):
            raise RuntimeError("another paperless-assistant instance holds the active lease")
        self._start_task(self._lease_loop())
        self._start_task(self._cleanup_loop())
        self._start_task(self._inbox_tag_cleanup_loop())
        self._start_task(
            self.discord.start(self.settings.discord_token.get_secret_value()),
            name="discord-gateway",
        )

    def _start_task(self, coroutine: Any, *, name: str | None = None) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.ready = False
            logger.error(
                "background_task_failed",
                extra={
                    "service": self.settings.app_name,
                    "ready": False,
                    "error_type": type(error).__name__,
                },
            )

    async def stop(self) -> None:
        """Stop Gateway work and release the single-worker lease."""
        self.ready = False
        await self.discord.close()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.repository.release_instance(self.instance_id)
        await self.repository.close()
        await self.paperless.close()

    async def _lease_loop(self) -> None:
        interval = max(5, self.settings.instance_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            if not await self.repository.acquire_instance(self.instance_id):
                raise RuntimeError("single-worker lease was lost")

    async def _cleanup_loop(self) -> None:
        while True:
            local_now = datetime.now().astimezone()
            next_run = local_now.replace(
                hour=self.settings.cleanup_hour_local,
                minute=0,
                second=0,
                microsecond=0,
            )
            if next_run <= local_now:
                next_run += timedelta(days=1)
            await asyncio.sleep((next_run - local_now).total_seconds())
            now = datetime.now(tz=UTC)
            question_hours = (
                self.settings.cleanup_question_delay_minutes / 60.0
                if self.settings.cleanup_question_delay_minutes > 0
                else float(self.settings.query_conversation_retention_hours)
            )
            context_before = (now - timedelta(hours=question_hours)).isoformat()
            failed_before = (
                now - timedelta(days=self.settings.failed_message_retention_days)
            ).isoformat()
            succeeded_hours = (
                self.settings.cleanup_upload_delay_minutes / 60.0
                if self.settings.cleanup_upload_delay_minutes > 0
                else 0.0
            )
            succeeded_before = (now - timedelta(hours=succeeded_hours)).isoformat()
            question_ids, upload_ids = await self.repository.cleanup_message_ids(
                context_before=context_before,
                succeeded_before=succeeded_before,
                failed_before=failed_before,
            )
            await self.discord.cleanup_messages(question_ids, upload_ids)
            await self.repository.purge(
                context_before=context_before,
                audit_before=(now - timedelta(days=self.settings.audit_retention_days)).isoformat(),
                failed_before=failed_before,
            )
            self._purge_delivery_spool(now.timestamp() - 3600)
            await self._purge_orphan_staging(now.timestamp() - 3600)

    async def _inbox_tag_cleanup_loop(self) -> None:
        interval = max(30, self.settings.cleanup_inbox_tag_poll_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            if not self.settings.cleanup_inbox_tag_enabled:
                continue
            try:
                upload_message_ids = await self.ingestion.check_inbox_tag_removals()
                if upload_message_ids:
                    await self.discord.cleanup_messages((), upload_message_ids)
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "inbox_tag_cleanup_failed",
                    extra={
                        "service": self.settings.app_name,
                        "error_type": type(error).__name__,
                    },
                )

    def _purge_delivery_spool(self, older_than: float) -> None:
        for path in self.settings.delivery_dir.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < older_than:
                    path.unlink()
            except OSError:
                logger.warning("delivery_spool_cleanup_failed")

    async def _purge_orphan_staging(self, older_than: float) -> None:
        protected = await self.repository.protected_staged_paths()
        for path in self.settings.staging_dir.iterdir():
            try:
                if path.is_file() and path not in protected and path.stat().st_mtime < older_than:
                    path.unlink()
            except OSError:
                logger.warning("staging_spool_cleanup_failed")


def _metadata(settings: Settings) -> dict[str, Any]:
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
        "runtime": "discord-gateway",
    }


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    start_worker: bool = True,
) -> Starlette:
    """Construct the private health application and worker lifecycle."""
    resolved = settings or Settings()
    resolved_runtime = runtime or (Runtime(resolved) if start_worker else None)

    async def service_metadata(_: Request) -> JSONResponse:
        return JSONResponse(_metadata(resolved))

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "service": resolved.app_name, "version": resolved.app_version}
        )

    async def ready(request: Request) -> JSONResponse:
        worker = request.app.state.runtime
        is_ready = bool(worker.ready) if worker is not None else bool(request.app.state.ready)
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
                "runtime": "discord-gateway",
            },
        )
        if resolved_runtime is not None:
            await resolved_runtime.start()
        else:
            application.state.ready = True
        try:
            yield
        finally:
            application.state.ready = False
            if resolved_runtime is not None:
                await resolved_runtime.stop()
            logger.info("service_shutdown", extra={"service": resolved.app_name, "ready": False})

    application = Starlette(
        debug=False,
        routes=[
            Route("/", service_metadata, methods=["GET"]),
            Route("/healthz", health, methods=["GET"]),
            Route("/readyz", ready, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    application.state.ready = False
    application.state.runtime = resolved_runtime
    return application


def main() -> None:
    """Run the primary worker and private health server."""
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
