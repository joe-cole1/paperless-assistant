"""Composition-root lifecycle, singleton, and cleanup tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from paperless_assistant import app as app_module
from paperless_assistant.app import Runtime, create_app, main
from paperless_assistant.config import Settings


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
