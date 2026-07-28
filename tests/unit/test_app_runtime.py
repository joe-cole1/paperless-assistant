"""Composition-root lifecycle, singleton, and cleanup tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from paperless_assistant import app as app_module
from paperless_assistant.app import Runtime, create_app, main
from paperless_assistant.config import Settings
from paperless_assistant.models import DiscordMessageTarget


@pytest.mark.asyncio
async def test_runtime_start_singleton_cleanup_and_stop(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data", instance_lease_seconds=15)
    runtime = Runtime(settings)
    cast(Any, runtime.discord).start = AsyncMock(return_value=None)
    cast(Any, runtime.discord).close = AsyncMock(return_value=None)
    cast(Any, runtime.paperless).close = AsyncMock(return_value=None)

    await runtime.start()
    assert settings.database_path.exists()
    assert settings.staging_dir.exists()
    assert settings.delivery_dir.exists()
    runtime._set_ready(True)
    assert runtime.ready

    old = settings.delivery_dir / "old"
    current = settings.delivery_dir / "current"
    old.write_bytes(b"old")
    current.write_bytes(b"current")
    os.utime(old, (1, 1))
    runtime._purge_delivery_spool(2)
    assert not old.exists()
    assert current.exists()

    orphan = settings.staging_dir / "orphan"
    protected = settings.staging_dir / "protected"
    orphan.write_bytes(b"orphan")
    protected.write_bytes(b"protected")
    os.utime(orphan, (1, 1))
    os.utime(protected, (1, 1))
    cast(Any, runtime.repository).protected_staged_paths = AsyncMock(
        return_value=frozenset({protected})
    )
    await runtime._purge_orphan_staging(2)
    assert not orphan.exists()
    assert protected.exists()

    second = Runtime(settings)
    cast(Any, second.discord).start = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="another paperless-assistant"):
        await second.start()
    await second.paperless.close()

    await runtime.stop()
    cast(Any, runtime.discord).close.assert_awaited_once()
    cast(Any, runtime.paperless).close.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_background_failure_degrades_readiness(
    settings_factory: Callable[..., Settings],
) -> None:
    runtime = Runtime(settings_factory())
    runtime.ready = True

    async def fail() -> None:
        raise RuntimeError("synthetic")

    runtime._start_task(fail(), name="synthetic")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not runtime.ready
    await runtime.paperless.close()
    await runtime.discord.close()


@pytest.mark.asyncio
async def test_runtime_successful_task_lease_loss_and_empty_stop(
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(settings_factory(instance_lease_seconds=15))
    runtime.ready = True

    async def succeed() -> None:
        return

    runtime._start_task(succeed(), name="synthetic-success")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime.ready

    cast(Any, runtime.repository).acquire_instance = AsyncMock(
        side_effect=[True, False],
    )
    monkeypatch.setattr("paperless_assistant.app.asyncio.sleep", AsyncMock())
    with pytest.raises(RuntimeError, match="lease was lost"):
        await runtime._lease_loop()

    cast(Any, runtime.discord).close = AsyncMock()
    cast(Any, runtime.repository).release_instance = AsyncMock()
    cast(Any, runtime.repository).close = AsyncMock()
    cast(Any, runtime.paperless).close = AsyncMock()
    await runtime.stop()
    cast(Any, runtime.repository).release_instance.assert_awaited_once_with(runtime.instance_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cleanup_hour", "expected_sleep"),
    [(13, 3600.0), (11, 23 * 3600.0)],
)
async def test_runtime_cleanup_loop_runs_daily_maintenance(
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
    cleanup_hour: int,
    expected_sleep: float,
) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Self:
            if tz is None:
                return cls(2026, 7, 26, 12)
            return cls(2026, 7, 26, 12, tzinfo=UTC)

    runtime = Runtime(settings_factory(cleanup_hour_local=cleanup_hour))
    question_target = DiscordMessageTarget(channel_id=1, message_id=10)
    upload_target = DiscordMessageTarget(channel_id=2, message_id=20)
    cast(Any, runtime.repository).cleanup_message_ids = AsyncMock(
        return_value=((question_target,), (upload_target,))
    )
    cast(Any, runtime.discord).cleanup_messages = AsyncMock(
        return_value=(question_target, upload_target)
    )
    cast(Any, runtime.repository).confirm_message_cleanup = AsyncMock()
    cast(Any, runtime.repository).purge = AsyncMock()
    cast(Any, runtime)._purge_delivery_spool = MagicMock()
    cast(Any, runtime)._purge_orphan_staging = AsyncMock()
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
    monkeypatch.setattr(app_module, "datetime", FixedDateTime)
    monkeypatch.setattr("paperless_assistant.app.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await runtime._cleanup_loop()

    assert sleep.await_args_list[0].args == (expected_sleep,)
    cast(Any, runtime.discord).cleanup_messages.assert_awaited_once_with(
        (question_target,), (upload_target,)
    )
    cast(Any, runtime.repository).confirm_message_cleanup.assert_awaited_once_with(
        (question_target, upload_target)
    )
    cast(Any, runtime.repository).purge.assert_awaited_once()
    await runtime.paperless.close()
    await runtime.discord.close()


@pytest.mark.asyncio
async def test_runtime_cleanup_logs_filesystem_failures(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = Runtime(settings_factory(data_dir=tmp_path))
    runtime.settings.delivery_dir.mkdir(parents=True)
    runtime.settings.staging_dir.mkdir(parents=True)
    delivery = runtime.settings.delivery_dir / "old"
    staging = runtime.settings.staging_dir / "orphan"
    delivery.write_bytes(b"old")
    staging.write_bytes(b"orphan")
    os.utime(delivery, (1, 1))
    os.utime(staging, (1, 1))
    cast(Any, runtime.repository).protected_staged_paths = AsyncMock(return_value=frozenset())

    def fail_unlink(_: Path, *, missing_ok: bool = False) -> None:
        del missing_ok
        raise OSError("synthetic")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    runtime._purge_delivery_spool(2)
    await runtime._purge_orphan_staging(2)

    assert "delivery_spool_cleanup_failed" in caplog.messages
    assert "staging_spool_cleanup_failed" in caplog.messages
    await runtime.paperless.close()
    await runtime.discord.close()


class FakeRuntime:
    def __init__(self) -> None:
        self.ready = True
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_app_lifecycle_with_runtime(
    settings_factory: Callable[..., Settings],
) -> None:
    runtime = FakeRuntime()
    application = create_app(
        settings_factory(),
        runtime=cast(Runtime, runtime),
    )

    async with application.router.lifespan_context(application):
        assert runtime.started
        assert cast(Any, application.state.runtime).ready

    assert runtime.stopped


def test_main_builds_uvicorn_app(
    monkeypatch: pytest.MonkeyPatch,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()
    run = MagicMock()
    monkeypatch.setattr(app_module, "Settings", lambda: settings)
    monkeypatch.setattr("paperless_assistant.app.uvicorn.run", run)

    main()

    run.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_inbox_tag_cleanup_loop(
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime(settings_factory(cleanup_inbox_tag_poll_interval_seconds=30))
    upload_target = DiscordMessageTarget(channel_id=2, message_id=100)
    cast(Any, runtime.ingestion).check_inbox_tag_removals = AsyncMock(
        side_effect=[(), (upload_target,), Exception("synthetic"), asyncio.CancelledError]
    )
    cast(Any, runtime.discord).cleanup_messages = AsyncMock(return_value=(upload_target,))
    cast(Any, runtime.repository).confirm_message_cleanup = AsyncMock()

    calls = 0

    async def toggle_sleep(_: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            runtime.settings = settings_factory(cleanup_inbox_tag_enabled=True)

    monkeypatch.setattr("paperless_assistant.app.asyncio.sleep", toggle_sleep)
    runtime.settings = settings_factory(cleanup_inbox_tag_enabled=False)

    with pytest.raises(asyncio.CancelledError):
        await runtime._inbox_tag_cleanup_loop()

    cast(Any, runtime.discord).cleanup_messages.assert_awaited_once_with((), (upload_target,))
    cast(Any, runtime.repository).confirm_message_cleanup.assert_awaited_once_with((upload_target,))
    await runtime.paperless.close()
    await runtime.discord.close()
