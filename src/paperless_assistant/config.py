"""Typed, non-secret health runtime configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from paperless_assistant import __version__

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Validated health runtime settings loaded from environment variables or ``.env``."""

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
