"""Unit tests for user credential storage, paperless token validation, and unlinked handling."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.errors import UnlinkedUserError
from paperless_assistant.models import IngestionJob, MetadataGuidance
from paperless_assistant.repository import SQLiteRepository
from paperless_assistant.services import (
    DeliveryService,
    IngestionService,
    QueryService,
    TaxonomyCache,
)


@pytest.mark.asyncio
async def test_repository_save_get_delete_user_tokens(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    repo = SQLiteRepository(
        db_path, lease_seconds=60, encryption_key=SecretStr("super-secret-key-12345")
    )
    await repo.initialize()

    # Initially missing
    token = await repo.get_user_token(201)
    assert token is None

    # Save token
    secret_token = SecretStr("token-user-201")
    await repo.save_user_token(201, secret_token)

    # Fetch token
    retrieved = await repo.get_user_token(201)
    assert retrieved is not None
    assert retrieved.get_secret_value() == "token-user-201"

    # Save another token
    await repo.save_user_token(202, SecretStr("token-user-202"))
    retrieved_202 = await repo.get_user_token(202)
    assert retrieved_202 is not None
    assert retrieved_202.get_secret_value() == "token-user-202"

    # Delete token
    deleted = await repo.delete_user_token(201)
    assert deleted is True
    assert await repo.get_user_token(201) is None

    # Delete non-existent token
    deleted_again = await repo.delete_user_token(201)
    assert deleted_again is False


@pytest.mark.asyncio
async def test_repository_handles_corrupted_tokens(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    repo = SQLiteRepository(db_path, lease_seconds=60, encryption_key=SecretStr("key1"))
    await repo.initialize()
    await repo.save_user_token(201, SecretStr("my-token"))

    # Instantiate repo with different encryption key
    repo_different_key = SQLiteRepository(
        db_path, lease_seconds=60, encryption_key=SecretStr("key2")
    )
    await repo_different_key.initialize()
    # Decryption should fail gracefully and return None
    assert await repo_different_key.get_user_token(201) is None


@pytest.mark.asyncio
async def test_services_raise_unlinked_user_error_when_token_missing(
    tmp_path: Path, settings_factory: Callable[..., Settings]
) -> None:
    db_path = tmp_path / "test.sqlite3"
    repo = SQLiteRepository(db_path, lease_seconds=60, encryption_key=SecretStr("test-key"))
    await repo.initialize()

    class FakeGateway:
        async def validate_token(self, token: object) -> bool:
            return True

    settings = settings_factory(data_dir=tmp_path / "data")
    gateway = cast(Any, FakeGateway())

    taxonomy = TaxonomyCache(settings, gateway)
    query = QueryService(settings, gateway, repo, repo, credentials=repo)
    ingestion = IngestionService(settings, gateway, repo, repo, taxonomy, credentials=repo)
    delivery = DeliveryService(settings, gateway, repo, credentials=repo)

    # User 201 has no token saved
    with pytest.raises(UnlinkedUserError):
        await query.ask(201, "What is my invoice total?")

    dummy_job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=1,
        principal_id=201,
        staged_path=tmp_path,
        original_filename="test.pdf",
        media_type="application/pdf",
        office_dependent=False,
        caption="",
        guidance=MetadataGuidance((1,), None, None),
    )
    with pytest.raises(UnlinkedUserError):
        await ingestion.submit(dummy_job)

    with pytest.raises(UnlinkedUserError):
        await delivery.prepare(201, 10, 10)


@pytest.mark.asyncio
async def test_repository_missing_encryption_key_raises_error(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    repo = SQLiteRepository(db_path, lease_seconds=60, encryption_key=None)
    await repo.initialize()

    with pytest.raises(ValueError, match="encryption key is not configured"):
        await repo.save_user_token(201, SecretStr("token"))

    with pytest.raises(ValueError, match="encryption key is not configured"):
        await repo.get_user_token(201)
