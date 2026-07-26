"""Unit tests for typed application configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from paperless_assistant import __version__
from paperless_assistant.config import Settings


def test_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.app_name == "paperless-assistant"
    assert settings.app_version == __version__
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


@pytest.mark.parametrize(
    ("environment", "value"),
    [
        ("PORT", "0"),
        ("PORT", "70000"),
        ("PORT", "not-a-port"),
        ("LOG_LEVEL", "verbose"),
        ("HOST", "https://localhost"),
        ("APP_ENV", " production "),
    ],
)
def test_invalid_configuration_is_rejected(
    monkeypatch: pytest.MonkeyPatch, environment: str, value: str
) -> None:
    monkeypatch.setenv(environment, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "value",
    ["bad host", "https://localhost", "localhost/path"],
)
def test_invalid_bind_host_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("HOST", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
