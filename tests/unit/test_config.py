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
    assert settings.mcp_bootstrap_mode is True
    assert settings.mcp_allowed_hosts == (
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    )


@pytest.mark.parametrize(
    ("environment", "value"),
    [
        ("PORT", "0"),
        ("PORT", "70000"),
        ("PORT", "not-a-port"),
        ("LOG_LEVEL", "verbose"),
        ("MCP_BOOTSTRAP_MODE", "perhaps"),
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


def test_comma_separated_allowlists_are_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "localhost:*, 127.0.0.1:8780")
    monkeypatch.setenv(
        "MCP_ALLOWED_ORIGINS", "http://localhost:*, https://paperless-mcp.example.com"
    )

    settings = Settings(_env_file=None)

    assert settings.mcp_allowed_hosts == ("localhost:*", "127.0.0.1:8780")
    assert settings.mcp_allowed_origins == (
        "http://localhost:*",
        "https://paperless-mcp.example.com",
    )


@pytest.mark.parametrize(
    ("environment", "value"),
    [
        ("MCP_ALLOWED_HOSTS", ""),
        ("MCP_ALLOWED_HOSTS", "https://localhost"),
        ("MCP_ALLOWED_ORIGINS", "*"),
        ("MCP_ALLOWED_ORIGINS", "https://example.com/path"),
        ("MCP_ALLOWED_ORIGINS", "https://bad host.example"),
        ("MCP_ALLOWED_ORIGINS", "https://example.com:99999"),
    ],
)
def test_invalid_allowlist_is_rejected(
    monkeypatch: pytest.MonkeyPatch, environment: str, value: str
) -> None:
    monkeypatch.setenv(environment, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
