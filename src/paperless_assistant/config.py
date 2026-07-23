"""Typed, non-secret application configuration."""

from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from paperless_assistant import __version__

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
CsvValues = Annotated[tuple[str, ...], NoDecode]

_HOST_PATTERN = re.compile(r"^(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(?::(?:\d+|\*))?$")


def _parse_csv(value: object) -> object:
    """Parse a comma-separated environment value into a non-empty tuple."""
    if not isinstance(value, str):
        return value
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if not values:
        raise ValueError("must contain at least one value")
    return values


class Settings(BaseSettings):
    """Validated runtime settings loaded from environment variables or ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
        validate_default=True,
    )

    app_env: str = Field(default="development", min_length=1, max_length=64)
    app_name: str = Field(default="paperless-assistant", min_length=1, max_length=128)
    app_version: str = Field(default=__version__, min_length=1, max_length=64)
    log_level: LogLevel = "INFO"
    host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    port: int = Field(default=8000, ge=1, le=65535)
    mcp_bootstrap_mode: bool = True
    mcp_allowed_hosts: CsvValues = (
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    )
    mcp_allowed_origins: CsvValues = (
        "http://localhost",
        "http://localhost:*",
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "http://[::1]",
        "http://[::1]:*",
    )

    _parse_hosts = field_validator("mcp_allowed_hosts", mode="before")(_parse_csv)
    _parse_origins = field_validator("mcp_allowed_origins", mode="before")(_parse_csv)

    @field_validator("app_env", "app_name", "app_version", "host", mode="before")
    @classmethod
    def reject_surrounding_whitespace(cls, value: object) -> object:
        """Reject ambiguous string values rather than silently normalizing them."""
        if isinstance(value, str) and value != value.strip():
            raise ValueError("must not contain surrounding whitespace")
        return value

    @field_validator("host")
    @classmethod
    def validate_bind_host(cls, value: str) -> str:
        """Require a host name or IP address, not a URL."""
        if "://" in value or "/" in value or any(character.isspace() for character in value):
            raise ValueError("must be a host name or IP address, not a URL")
        return value

    @field_validator("mcp_allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Validate Host-header allowlist entries accepted by the MCP SDK."""
        if not values:
            raise ValueError("must contain at least one host")
        invalid = [value for value in values if not _HOST_PATTERN.fullmatch(value)]
        if invalid:
            raise ValueError(f"invalid allowed host value: {invalid[0]}")
        return values

    @field_validator("mcp_allowed_origins")
    @classmethod
    def validate_allowed_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Validate exact or wildcard-port HTTP origins."""
        if not values:
            raise ValueError("must contain at least one origin")
        for origin in values:
            candidate = origin.removesuffix(":*")
            try:
                parsed = urlsplit(candidate)
                parsed_port = parsed.port
            except ValueError as error:
                raise ValueError(f"invalid allowed origin value: {origin}") from error
            if (
                any(character.isspace() for character in origin)
                or "*" in candidate
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.hostname is None
                or parsed_port == 0
                or parsed.netloc.endswith(":")
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "@" in parsed.netloc
            ):
                raise ValueError(f"invalid allowed origin value: {origin}")
        return values
