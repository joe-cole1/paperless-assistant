"""Shared synthetic settings and paths."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from paperless_assistant.config import Settings


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., Settings]:
    """Build fully valid settings without reading developer environment."""

    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "discord_token": "synthetic-discord-token",
            "discord_guild_id": 100,
            "discord_questions_channel_id": 101,
            "discord_uploads_channel_id": 102,
            "discord_allowed_user_ids": frozenset({201, 202}),
            "paperless_internal_url": "http://paperless.test",
            "paperless_public_url": "https://paperless.example.test",
            "paperless_token": "synthetic-paperless-token",
            "data_dir": tmp_path / "data",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return factory
