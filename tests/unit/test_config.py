"""Unit tests for fail-closed application configuration."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from paperless_assistant import __version__
from paperless_assistant.config import MIB, Settings


def test_safe_defaults(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory()

    assert settings.app_env == "development"
    assert settings.app_name == "paperless-assistant"
    assert settings.app_version == __version__
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.discord_allowed_user_ids == frozenset({201, 202})
    assert (
        settings.encryption_key.get_secret_value() == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    assert settings.discord_max_attachment_bytes == 25 * MIB
    assert settings.ingestion_max_staged_bytes == 100 * MIB
    assert settings.context_ttl.total_seconds() == 900
    assert settings.database_path.name == "assistant.sqlite3"
    assert settings.staging_dir.name == "staging"
    assert settings.delivery_dir.name == "delivery"
    assert settings.paperless_token.get_secret_value() == "synthetic-paperless-token"
    assert settings.cleanup_inbox_tag == "inbox"
    assert settings.cleanup_inbox_tag_enabled is True
    assert settings.cleanup_inbox_tag_poll_interval_seconds == 300
    assert settings.cleanup_question_delay_minutes == 0
    assert settings.cleanup_upload_delay_minutes == 0
    assert settings.allow_edit_title is True
    assert settings.allow_edit_date is True
    assert settings.allow_edit_correspondent is True
    assert settings.allow_edit_document_type is True
    assert settings.allow_edit_storage_path is True
    assert settings.allow_edit_tags is True
    assert settings.require_new_metadata_confirmation is True
    assert settings.ai_review_completion_tag is None


def test_ai_review_flags_parse_compose_boolean_strings(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        allow_edit_title="false",
        allow_edit_date="false",
        allow_edit_correspondent="false",
        allow_edit_document_type="false",
        allow_edit_storage_path="false",
        allow_edit_tags="false",
        require_new_metadata_confirmation="false",
    )

    assert settings.allow_edit_title is False
    assert settings.allow_edit_date is False
    assert settings.allow_edit_correspondent is False
    assert settings.allow_edit_document_type is False
    assert settings.allow_edit_storage_path is False
    assert settings.allow_edit_tags is False
    assert settings.require_new_metadata_confirmation is False


def test_optional_ai_review_completion_tag_parses_blank_as_disabled(
    settings_factory: Callable[..., Settings],
) -> None:
    assert settings_factory(ai_review_completion_tag="").ai_review_completion_tag is None


def test_required_secrets_and_ids_have_no_defaults() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("port", 0),
        ("port", 70000),
        ("log_level", "verbose"),
        ("host", "https://localhost"),
        ("app_env", " production "),
        ("discord_allowed_user_ids", frozenset()),
        ("discord_allowed_user_ids", frozenset({0})),
        ("encryption_key", "guessable-passphrase"),
        ("data_dir", "relative"),
        ("paperless_public_url", "http://paperless.example.test"),
        ("paperless_source_tag", " Discord "),
        ("cleanup_inbox_tag", " inbox "),
        ("ai_review_completion_tag", " reviewed "),
    ],
)
def test_invalid_configuration_is_rejected(
    settings_factory: Callable[..., Settings], field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        settings_factory(**{field: value})


def test_related_limits_are_validated(settings_factory: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="channels must be different"):
        settings_factory(discord_uploads_channel_id=101)
    with pytest.raises(ValidationError, match="at least the per-file limit"):
        settings_factory(ingestion_max_staged_bytes=10)
    with pytest.raises(ValidationError, match="initial task poll"):
        settings_factory(
            paperless_task_poll_initial_seconds=20,
            paperless_task_poll_max_seconds=10,
        )
    with pytest.raises(ValidationError, match="must differ"):
        settings_factory(ai_review_completion_tag="INBOX")
