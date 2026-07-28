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
from paperless_assistant.models import IngestionJob, JobState, MetadataGuidance
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
        db_path,
        lease_seconds=60,
        encryption_key=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
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
    repo = SQLiteRepository(
        db_path,
        lease_seconds=60,
        encryption_key=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
    )
    await repo.initialize()
    await repo.save_user_token(201, SecretStr("my-token"))

    # Instantiate repo with different encryption key
    repo_different_key = SQLiteRepository(
        db_path,
        lease_seconds=60,
        encryption_key=SecretStr("BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="),
    )
    await repo_different_key.initialize()
    # Decryption should fail gracefully and return None
    assert await repo_different_key.get_user_token(201) is None


@pytest.mark.asyncio
async def test_services_raise_unlinked_user_error_when_token_missing(
    tmp_path: Path, settings_factory: Callable[..., Settings]
) -> None:
    db_path = tmp_path / "test.sqlite3"
    repo = SQLiteRepository(
        db_path,
        lease_seconds=60,
        encryption_key=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
    )
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

    staged_path = tmp_path / "unlinked.pdf"
    staged_path.write_bytes(b"%PDF-1.7")
    with pytest.raises(UnlinkedUserError):
        await ingestion.stage(
            discord_message_id=1,
            discord_attachment_id=1,
            principal_id=201,
            staged_path=staged_path,
            original_filename="synthetic.pdf",
            caption="",
        )
    assert await repo.recoverable_jobs() == ()

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
async def test_revoked_credentials_fail_and_clean_durable_jobs(
    tmp_path: Path, settings_factory: Callable[..., Settings]
) -> None:
    db_path = tmp_path / "test.sqlite3"
    repo = SQLiteRepository(
        db_path,
        lease_seconds=60,
        encryption_key=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
    )
    await repo.initialize()

    class FailIfCalledGateway:
        async def submit_document(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("revoked credentials must prevent submission")

        async def get_task(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("revoked credentials must prevent polling")

    settings = settings_factory(data_dir=tmp_path / "data")
    taxonomy = TaxonomyCache(settings, cast(Any, FailIfCalledGateway()))
    ingestion = IngestionService(
        settings,
        cast(Any, FailIfCalledGateway()),
        repo,
        repo,
        taxonomy,
        credentials=repo,
    )

    staged_path = tmp_path / "staged.pdf"
    staged_path.write_bytes(b"%PDF-1.7")
    staged = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=1,
        principal_id=201,
        staged_path=staged_path,
        original_filename="synthetic.pdf",
        media_type="application/pdf",
        office_dependent=False,
        caption="",
        guidance=MetadataGuidance((1,), None, None),
    )
    assert await repo.create_job(staged)
    assert (await ingestion.submit(staged)).job.state == JobState.FAILED
    assert not staged_path.exists()

    submitted_path = tmp_path / "submitted.pdf"
    submitted_path.write_bytes(b"%PDF-1.7")
    submitted = IngestionJob(
        id=uuid4(),
        discord_message_id=2,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=submitted_path,
        original_filename="synthetic.pdf",
        media_type="application/pdf",
        office_dependent=False,
        caption="",
        guidance=MetadataGuidance((1,), None, None),
    )
    assert await repo.create_job(submitted)
    assert await repo.transition_job(submitted.id, JobState.STAGED, JobState.SUBMITTING)
    task_id = uuid4()
    assert await repo.transition_job(
        submitted.id,
        JobState.SUBMITTING,
        JobState.SUBMITTED,
        task_id=task_id,
    )
    durable = await repo.get_job(submitted.id)
    assert durable is not None
    assert (await ingestion.poll_once(durable)).job.state == JobState.FAILED
    assert not submitted_path.exists()


@pytest.mark.asyncio
async def test_repository_missing_encryption_key_raises_error(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    repo = SQLiteRepository(db_path, lease_seconds=60, encryption_key=None)
    await repo.initialize()

    with pytest.raises(ValueError, match="encryption key is not configured"):
        await repo.save_user_token(201, SecretStr("token"))

    with pytest.raises(ValueError, match="encryption key is not configured"):
        await repo.get_user_token(201)
