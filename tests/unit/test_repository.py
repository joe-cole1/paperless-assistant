"""SQLite durability, idempotency, transition, and privacy tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from paperless_assistant.models import (
    AuditEvent,
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
        connection.executescript(SCHEMA.replace("    discord_status_message_id INTEGER,\n", ""))
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
    assert await repository.active_succeeded_uploads() == ((50, 42),)
    assert await repository.message_job_states(10) == (JobState.SUCCEEDED,)
    assert await repository.message_job_states(999) == ()
    future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    assert await repository.cleanup_message_ids(
        context_before=future,
        succeeded_before=future,
        failed_before=future,
    ) == ((), (10, 50))
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
    ) == ((), (10, 50))
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
    question_ids, upload_ids = await repository.cleanup_message_ids(
        context_before=old,
        succeeded_before=old,
        failed_before=old,
    )
    assert set(question_ids) == {70, 80, 81, 82}
    assert upload_ids == ()
    await repository.purge(context_before=old, audit_before=old, failed_before=old)
    assert await repository.get_context(30) is None
    assert [action async for action in repository.actions()] == []
