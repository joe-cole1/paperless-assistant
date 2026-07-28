"""SQLite durability, idempotency, transition, and privacy tests."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from paperless_assistant.models import (
    AuditEvent,
    DiscordMessageTarget,
    DocumentId,
    IngestionJob,
    JobState,
    MetadataGuidance,
    ReferenceContext,
)
from paperless_assistant.repository import SCHEMA, SQLiteRepository


def _job(path: Path, *, message_id: int = 10, attachment_id: int = 20) -> IngestionJob:
    return IngestionJob(
        id=uuid4(),
        discord_message_id=message_id,
        discord_attachment_id=attachment_id,
        discord_status_message_id=50,
        discord_message_channel_id=102,
        discord_status_channel_id=500,
        principal_id=30,
        staged_path=path,
        original_filename="synthetic.pdf",
        media_type="application/pdf",
        office_dependent=False,
        caption="private caption",
        guidance=MetadataGuidance((1, 2), 3, 4),
    )


@pytest.mark.asyncio
async def test_single_instance_lease_and_permissions(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "state" / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    first = uuid4()
    second = uuid4()

    assert await repository.acquire_instance(first)
    assert await repository.acquire_instance(first)
    assert not await repository.acquire_instance(second)
    await repository.release_instance(second)
    assert not await repository.acquire_instance(second)
    await repository.release_instance(first)
    assert await repository.acquire_instance(second)
    assert (tmp_path / "state").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "state" / "db.sqlite3").stat().st_mode & 0o777 == 0o600
    await repository.close()


@pytest.mark.asyncio
async def test_initialize_migrates_status_message_column(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    try:
        legacy_schema = SCHEMA.replace("    discord_status_message_id INTEGER,\n", "")
        legacy_schema = legacy_schema.replace("    discord_message_channel_id INTEGER,\n", "")
        legacy_schema = legacy_schema.replace("    discord_status_channel_id INTEGER,\n", "")
        legacy_schema = legacy_schema.replace(
            "    discord_message_cleaned INTEGER NOT NULL DEFAULT 0,\n",
            "",
        )
        legacy_schema = legacy_schema.replace(
            "    discord_status_message_cleaned INTEGER NOT NULL DEFAULT 0,\n",
            "",
        )
        legacy_schema = legacy_schema.replace("    channel_id INTEGER,\n", "")
        connection.executescript(legacy_schema)
    finally:
        connection.close()

    repository = SQLiteRepository(database, lease_seconds=60)
    await repository.initialize()

    connection = sqlite3.connect(database)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ingestion_jobs)")}
    finally:
        connection.close()
    assert "discord_status_message_id" in columns
    assert "discord_message_channel_id" in columns
    assert "discord_status_channel_id" in columns
    assert "discord_message_cleaned" in columns
    assert "discord_status_message_cleaned" in columns
    connection = sqlite3.connect(database)
    try:
        question_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(question_messages)")
        }
    finally:
        connection.close()
    assert "channel_id" in question_columns
    assert await repository.get_warning_state() is None
    warning_time = datetime.now(tz=UTC)
    await repository.save_warning_state(90, warning_time)
    assert await repository.get_warning_state() == (90, warning_time)
    await repository.clear_warning_state()
    assert await repository.get_warning_state() is None


@pytest.mark.asyncio
async def test_job_idempotency_roundtrip_and_transitions(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    job = _job(tmp_path / "staged")

    assert await repository.create_job(job)
    assert not await repository.create_job(_job(tmp_path / "other"))
    loaded = await repository.get_job(job.id)
    assert loaded is not None
    assert loaded.guidance == job.guidance
    assert loaded.discord_status_message_id == 50
    assert loaded.caption == "private caption"
    assert loaded.state == JobState.STAGED
    assert await repository.transition_job(job.id, JobState.STAGED, JobState.SUBMITTING)
    assert not await repository.transition_job(job.id, JobState.STAGED, JobState.FAILED)
    task_id = uuid4()
    assert await repository.transition_job(
        job.id,
        JobState.SUBMITTING,
        JobState.SUBMITTED,
        task_id=task_id,
    )
    assert await repository.transition_job(
        job.id,
        JobState.SUBMITTED,
        JobState.SUCCEEDED,
        document_id=42,
    )
    loaded = await repository.get_job(job.id)
    assert loaded is not None
    assert loaded.paperless_task_id == task_id
    assert loaded.paperless_document_id == DocumentId(42)
    assert loaded.state == JobState.SUCCEEDED
    upload_targets = (
        DiscordMessageTarget(102, 10),
        DiscordMessageTarget(500, 50),
    )
    assert await repository.active_succeeded_uploads() == ((upload_targets, 42),)
    no_channel_job = replace(
        _job(tmp_path / "no-channel", message_id=11, attachment_id=21),
        discord_status_message_id=None,
        discord_message_channel_id=None,
        discord_status_channel_id=None,
    )
    assert await repository.create_job(no_channel_job)
    assert await repository.transition_job(
        no_channel_job.id,
        JobState.STAGED,
        JobState.SUBMITTING,
    )
    assert await repository.transition_job(
        no_channel_job.id,
        JobState.SUBMITTING,
        JobState.SUBMITTED,
        task_id=uuid4(),
    )
    assert await repository.transition_job(
        no_channel_job.id,
        JobState.SUBMITTED,
        JobState.SUCCEEDED,
        document_id=43,
    )
    assert ((), 43) in await repository.active_succeeded_uploads()
    assert await repository.message_job_states(10) == (JobState.SUCCEEDED,)
    assert await repository.message_job_states(999) == ()
    future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    assert await repository.cleanup_message_ids(
        context_before=future,
        succeeded_before=future,
        failed_before=future,
    ) == ((), upload_targets)
    failed_sibling = _job(tmp_path / "failed", attachment_id=21)
    assert await repository.create_job(failed_sibling)
    assert await repository.transition_job(failed_sibling.id, JobState.STAGED, JobState.FAILED)
    past = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat()
    assert await repository.cleanup_message_ids(
        context_before=future,
        succeeded_before=future,
        failed_before=past,
    ) == ((), ())
    assert await repository.cleanup_message_ids(
        context_before=future,
        succeeded_before=future,
        failed_before=future,
    ) == ((), upload_targets)
    await repository.purge(
        expired_before=past,
        audit_before=past,
        succeeded_before=future,
        failed_before=future,
    )
    assert await repository.get_job(job.id) is not None
    await repository.confirm_message_cleanup((upload_targets[0],))
    await repository.purge(
        expired_before=past,
        audit_before=past,
        succeeded_before=future,
        failed_before=future,
    )
    assert await repository.get_job(job.id) is not None
    await repository.confirm_message_cleanup((upload_targets[1],))
    await repository.purge(
        expired_before=past,
        audit_before=past,
        succeeded_before=future,
        failed_before=future,
    )
    assert await repository.get_job(job.id) is None
    with pytest.raises(ValueError, match="invalid job transition"):
        await repository.transition_job(job.id, JobState.SUCCEEDED, JobState.STAGED)
    assert await repository.get_job(uuid4()) is None


@pytest.mark.asyncio
async def test_recovery_context_expiry_audit_and_purge(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    repository = SQLiteRepository(database, lease_seconds=60)
    await repository.initialize()
    staged = _job(tmp_path / "one", message_id=1, attachment_id=1)
    submitting = _job(tmp_path / "two", message_id=2, attachment_id=2)
    submitted = _job(tmp_path / "three", message_id=3, attachment_id=3)
    for job in (staged, submitting, submitted):
        assert await repository.create_job(job)
    await repository.transition_job(submitting.id, JobState.STAGED, JobState.SUBMITTING)
    await repository.transition_job(submitted.id, JobState.STAGED, JobState.SUBMITTING)
    await repository.transition_job(
        submitted.id,
        JobState.SUBMITTING,
        JobState.SUBMITTED,
        task_id=uuid4(),
    )
    assert {job.state for job in await repository.recoverable_jobs()} == {
        JobState.STAGED,
        JobState.SUBMITTING,
        JobState.SUBMITTED,
    }
    assert await repository.protected_staged_paths() == frozenset(
        {staged.staged_path, submitting.staged_path, submitted.staged_path}
    )

    future = datetime.now(tz=UTC) + timedelta(minutes=5)
    await repository.save_context(
        ReferenceContext(30, (DocumentId(7), DocumentId(8)), future, (70, 80))
    )
    context = await repository.get_context(30)
    assert context is not None
    assert context.document_ids == (DocumentId(7), DocumentId(8))
    await repository.save_context(ReferenceContext(30, (DocumentId(9),), future, (82,)))
    context = await repository.get_context(30)
    assert context is not None
    assert context.source_message_ids == (82,)
    await repository.save_context(
        ReferenceContext(
            31,
            (DocumentId(9),),
            datetime.now(tz=UTC) - timedelta(seconds=1),
            (81,),
        )
    )
    assert await repository.get_context(31) is None
    assert await repository.get_context(999) is None

    event = AuditEvent(
        principal_id=30,
        action="delivery",
        outcome="prepared",
        occurred_at=datetime.now(tz=UTC),
        correlation_id=uuid4(),
        job_id=staged.id,
        task_id=uuid4(),
        document_id=DocumentId(7),
        delivery_method="attachment",
    )
    await repository.record(event)
    assert [action async for action in repository.actions()] == ["delivery"]
    connection = sqlite3.connect(database)
    try:
        cursor = connection.execute("SELECT * FROM audit_events")
        columns = tuple(item[0] for item in cursor.description or ())
        row = cursor.fetchone()
    finally:
        connection.close()
    assert row is not None
    serialized = repr(dict(zip(columns, row, strict=True)))
    for forbidden in ("private caption", "synthetic.pdf", "token", "ocr"):
        assert forbidden not in serialized.casefold()

    old = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    question_targets, upload_targets = await repository.cleanup_message_ids(
        context_before=old,
        succeeded_before=old,
        failed_before=old,
    )
    assert set(question_targets) == {
        DiscordMessageTarget(30, 70),
        DiscordMessageTarget(30, 80),
        DiscordMessageTarget(30, 82),
        DiscordMessageTarget(31, 81),
    }
    assert upload_targets == ()
    await repository.confirm_message_cleanup(question_targets)
    await repository.purge(
        expired_before=old,
        audit_before=old,
        succeeded_before=old,
        failed_before=old,
    )
    assert await repository.get_context(30) is None
    assert [action async for action in repository.actions()] == []
