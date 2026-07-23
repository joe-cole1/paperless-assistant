"""Unit tests for the bootstrap system tool."""

from paperless_assistant.config import Settings
from paperless_assistant.tools.system import ping


def test_ping_result_schema() -> None:
    result = ping(Settings(_env_file=None))

    assert result.model_dump() == {
        "status": "ok",
        "message": "Paperless MCP connection successful",
        "service": "paperless-assistant",
        "version": "0.1.0",
        "bootstrap_mode": True,
    }


def test_ping_reflects_configured_public_metadata() -> None:
    settings = Settings(
        _env_file=None,
        app_name="custom-assistant",
        app_version="9.8.7",
        mcp_bootstrap_mode=False,
    )

    result = ping(settings)

    assert result.service == "custom-assistant"
    assert result.version == "9.8.7"
    assert result.bootstrap_mode is False
