"""Validated runtime configuration for the Discord-first assistant."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Literal, Self

from cryptography.fernet import Fernet
from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from paperless_assistant import __version__

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

MIB = 1024 * 1024


class Settings(BaseSettings):
    """Fail-closed settings loaded from environment variables or ``.env``."""

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
    tz: str = Field(default="UTC", min_length=1, max_length=128)

    discord_token: SecretStr = Field(min_length=1)
    discord_guild_id: int = Field(gt=0)
    discord_questions_channel_id: int = Field(gt=0)
    discord_uploads_channel_id: int = Field(gt=0)
    discord_allowed_user_ids: frozenset[int] = Field(default_factory=frozenset)
    encryption_key: SecretStr = Field(min_length=1)
    discord_max_attachments: int = Field(default=10, ge=1, le=10)
    discord_max_attachment_bytes: int = Field(default=25 * MIB, ge=1)

    paperless_internal_url: AnyHttpUrl
    paperless_public_url: AnyHttpUrl
    paperless_token: SecretStr = Field(min_length=1)
    paperless_api_version: int = Field(default=10, ge=9, le=10)
    paperless_source_tag: str = Field(default="Discord", min_length=1, max_length=100)
    paperless_office_uploads_enabled: bool = False

    data_dir: Path = Path("/data")
    ingestion_max_staged_bytes: int = Field(default=100 * MIB, ge=1)
    delivery_min_free_bytes: int = Field(default=50 * MIB, ge=0)

    paperless_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    paperless_read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    paperless_write_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    paperless_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    paperless_chat_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    paperless_ai_suggestions_timeout_seconds: float = Field(default=150.0, ge=121, le=600)
    paperless_native_task_notification_timeout_seconds: int = Field(
        default=15 * 60, ge=30, le=24 * 60 * 60
    )
    paperless_office_task_notification_timeout_seconds: int = Field(
        default=30 * 60, ge=30, le=24 * 60 * 60
    )
    paperless_task_poll_initial_seconds: float = Field(default=1.0, gt=0, le=60)
    paperless_task_poll_max_seconds: float = Field(default=15.0, gt=0, le=300)
    paperless_task_recovery_interval_seconds: int = Field(default=60, ge=5, le=3600)
    paperless_taxonomy_refresh_seconds: int = Field(default=300, ge=30, le=3600)

    question_global_concurrency: int = Field(default=2, ge=1, le=10)
    question_user_rate_limit: int = Field(default=10, ge=1, le=100)
    question_user_rate_window_seconds: int = Field(default=300, ge=10, le=3600)
    reference_context_ttl_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    suggestion_review_timeout_seconds: int = Field(default=15 * 60, ge=60, le=24 * 60 * 60)
    allow_edit_title: bool = True
    allow_edit_date: bool = True
    allow_edit_correspondent: bool = True
    allow_edit_document_type: bool = True
    allow_edit_storage_path: bool = True
    allow_edit_tags: bool = True
    require_new_metadata_confirmation: bool = True

    cleanup_hour_local: int = Field(default=3, ge=0, le=23)
    cleanup_inbox_tag: str = Field(default="inbox", min_length=1, max_length=100)
    cleanup_inbox_tag_enabled: bool = True
    cleanup_inbox_tag_poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    cleanup_question_delay_minutes: int = Field(default=0, ge=0, le=10080)
    cleanup_upload_delay_minutes: int = Field(default=0, ge=0, le=10080)
    query_conversation_retention_hours: int = Field(default=24, ge=1, le=168)
    failed_message_retention_days: int = Field(default=7, ge=1, le=90)
    audit_retention_days: int = Field(default=90, ge=7, le=3650)
    instance_lease_seconds: int = Field(default=60, ge=15, le=300)

    @field_validator(
        "app_env",
        "app_name",
        "app_version",
        "host",
        "tz",
        "paperless_source_tag",
        "cleanup_inbox_tag",
        mode="before",
    )
    @classmethod
    def reject_surrounding_whitespace(cls, value: object) -> object:
        """Reject ambiguous values rather than silently normalizing them."""
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

    @field_validator("discord_allowed_user_ids")
    @classmethod
    def validate_user_ids(cls, value: frozenset[int]) -> frozenset[int]:
        """Require an explicit, non-empty set of positive Discord snowflakes."""
        if not value:
            raise ValueError("must contain at least one authorized Discord user ID")
        if any(identifier <= 0 for identifier in value):
            raise ValueError("must contain only positive Discord user IDs")
        return value

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        """Require a generated Fernet key instead of a guessable passphrase."""
        try:
            Fernet(value.get_secret_value().encode("ascii"))
        except (UnicodeEncodeError, ValueError) as error:
            raise ValueError(
                "must be a URL-safe base64 Fernet key; generate one with Fernet.generate_key()"
            ) from error
        return value

    @field_validator("data_dir")
    @classmethod
    def validate_data_dir(cls, value: Path) -> Path:
        """Use one explicit absolute writable-state root."""
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value

    @model_validator(mode="after")
    def validate_related_limits(self) -> Self:
        """Validate cross-field safety constraints."""
        if self.discord_questions_channel_id == self.discord_uploads_channel_id:
            raise ValueError("Discord questions and uploads channels must be different")
        if self.ingestion_max_staged_bytes < self.discord_max_attachment_bytes:
            raise ValueError("INGESTION_MAX_STAGED_BYTES must be at least the per-file limit")
        if self.paperless_task_poll_initial_seconds > self.paperless_task_poll_max_seconds:
            raise ValueError("initial task poll interval must not exceed maximum")
        if self.paperless_public_url.scheme != "https":
            raise ValueError("PAPERLESS_PUBLIC_URL must use HTTPS")
        return self

    @property
    def database_path(self) -> Path:
        """Return the SQLite database location."""
        return self.data_dir / "assistant.sqlite3"

    @property
    def staging_dir(self) -> Path:
        """Return the restricted upload-staging directory."""
        return self.data_dir / "staging"

    @property
    def delivery_dir(self) -> Path:
        """Return the restricted download-spool directory."""
        return self.data_dir / "delivery"

    @property
    def context_ttl(self) -> timedelta:
        """Return the configured reference lifetime."""
        return timedelta(seconds=self.reference_context_ttl_seconds)
