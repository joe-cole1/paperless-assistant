"""SQLite durability, idempotency, transition, and privacy tests."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from paperless_assistant.models import (
    AuditEvent,
    ConversationTranscript,
    ConversationTurn,
    DiscordMessageTarget,
    DocumentId,
    IngestionJob,
    JobState,
    MetadataGuidance,
    ReferenceContext,
    ReviewFinalizationState,
    SearchMode,
    SearchSession,
    UploadBatch,
    UploadItem,
    UploadItemState,
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


def _encrypted_repository(database: Path) -> SQLiteRepository:
    return SQLiteRepository(
        database,
        lease_seconds=60,
        encryption_key=SecretStr(Fernet.generate_key().decode("ascii")),
    )


def _transcript(
    *, guild_id: int = 1, thread_id: int = 2, expired: bool = False
) -> ConversationTranscript:
    expiry = datetime.now(tz=UTC) + timedelta(minutes=5)
    if expired:
        expiry = datetime.now(tz=UTC) - timedelta(seconds=1)
    return ConversationTranscript(
        guild_id,
        thread_id,
        (ConversationTurn("question-marker", "answer-marker"),),
        expiry,
    )


def _session(
    *,
    session_id: UUID | None = None,
    guild_id: int = 1,
    thread_id: int = 2,
    created_at: datetime | None = None,
    expired: bool = False,
) -> SearchSession:
    created = created_at or datetime.now(tz=UTC)
    expiry = created + timedelta(minutes=5)
    if expired:
        expiry = datetime.now(tz=UTC) - timedelta(seconds=1)
    return SearchSession(
        session_id if session_id is not None else uuid4(),
        guild_id,
        thread_id,
        3,
        (DocumentId(9), DocumentId(4)),
        (101, 102, 103),
        (DiscordMessageTarget(2, 201), DiscordMessageTarget(2, 202)),
        104,
        SearchMode.TITLE,
        1,
        created,
        expiry,
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
async def test_slow_sqlite_operation_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    operation_started = threading.Event()
    release_operation = threading.Event()
    original_connection = repository._connection

    @contextmanager
    def delayed_connection() -> Iterator[sqlite3.Connection]:
        operation_started.set()
        if not release_operation.wait(timeout=0.5):
            raise TimeoutError("test did not release SQLite operation")
        with original_connection() as connection:
            yield connection

    monkeypatch.setattr(repository, "_connection", delayed_connection)
    database_call = asyncio.create_task(repository.get_warning_state())
    await asyncio.sleep(0.02)

    assert operation_started.is_set()
    assert not database_call.done()
    release_operation.set()
    assert await database_call is None
    await repository.close()


@pytest.mark.asyncio
async def test_concurrent_repository_calls_use_one_database_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _encrypted_repository(tmp_path / "db.sqlite3")
    connection_threads: list[int] = []
    original_connection = repository._connection

    @contextmanager
    def observed_connection() -> Iterator[sqlite3.Connection]:
        connection_threads.append(threading.get_ident())
        with original_connection() as connection:
            yield connection

    monkeypatch.setattr(repository, "_connection", observed_connection)
    await repository.initialize()
    await asyncio.gather(
        repository.get_warning_state(),
        repository.save_warning_state(10, datetime.now(tz=UTC)),
        repository.clear_warning_state(),
        repository.save_transcript(_transcript()),
        repository.save_search_session(_session()),
    )

    assert len(connection_threads) == 6
    assert set(connection_threads) == {connection_threads[0]}
    assert connection_threads[0] != threading.get_ident()
    await repository.close()


@pytest.mark.asyncio
async def test_close_drains_queued_operations_and_rejects_new_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    operation_started = threading.Event()
    release_operation = threading.Event()
    original_connection = repository._connection

    @contextmanager
    def delayed_connection() -> Iterator[sqlite3.Connection]:
        operation_started.set()
        if not release_operation.wait(timeout=0.5):
            raise TimeoutError("test did not release SQLite operation")
        with original_connection() as connection:
            yield connection

    monkeypatch.setattr(repository, "_connection", delayed_connection)
    first_call = asyncio.create_task(repository.get_warning_state())
    await asyncio.sleep(0.02)
    second_call = asyncio.create_task(repository.save_warning_state(10, datetime.now(tz=UTC)))
    close_call = asyncio.create_task(repository.close())
    await asyncio.sleep(0.02)

    assert operation_started.is_set()
    assert not first_call.done()
    assert not second_call.done()
    assert not close_call.done()
    release_operation.set()
    await asyncio.gather(first_call, second_call, close_call)
    with pytest.raises(RuntimeError, match="closed"):
        await repository.get_warning_state()
    await repository.close()


@pytest.mark.asyncio
async def test_concurrent_job_transitions_preserve_compare_and_swap(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    job = _job(tmp_path / "staged")
    assert await repository.create_job(job)

    transitions = await asyncio.gather(
        repository.transition_job(job.id, JobState.STAGED, JobState.SUBMITTING),
        repository.transition_job(job.id, JobState.STAGED, JobState.SUBMITTING),
    )
    loaded = await repository.get_job(job.id)

    assert transitions.count(True) == 1
    assert transitions.count(False) == 1
    assert loaded is not None
    assert loaded.state is JobState.SUBMITTING
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
        legacy_schema = legacy_schema.replace(
            "    duplicate_confirmed INTEGER NOT NULL DEFAULT 0,\n",
            "",
        )
        legacy_schema = legacy_schema.replace(
            "    review_finalization_state TEXT NOT NULL DEFAULT 'not_started',\n",
            "",
        )
        legacy_schema = legacy_schema.replace("    channel_id INTEGER,\n", "")
        for column in (
            "    title_message_id INTEGER,\n",
            "    metadata_message_id INTEGER,\n",
            "    actions_message_id INTEGER,\n",
            "    controls_message_id INTEGER,\n",
            "    review_finalization_state TEXT NOT NULL DEFAULT 'not_started',\n",
            "    parent_cleaned INTEGER NOT NULL DEFAULT 0,\n",
            "    thread_cleaned INTEGER NOT NULL DEFAULT 0,\n",
        ):
            legacy_schema = legacy_schema.replace(column, "")
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
    assert {"duplicate_confirmed", "review_finalization_state"} <= columns
    connection = sqlite3.connect(database)
    try:
        question_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(question_messages)")
        }
    finally:
        connection.close()
    assert "channel_id" in question_columns
    connection = sqlite3.connect(database)
    try:
        upload_columns = {row[1] for row in connection.execute("PRAGMA table_info(upload_items)")}
    finally:
        connection.close()
    assert {
        "title_message_id",
        "metadata_message_id",
        "actions_message_id",
        "controls_message_id",
        "review_finalization_state",
        "parent_cleaned",
        "thread_cleaned",
    } <= upload_columns
    assert await repository.get_warning_state() is None
    warning_time = datetime.now(tz=UTC)
    await repository.save_warning_state(90, warning_time)
    assert await repository.get_warning_state() == (90, warning_time)
    await repository.clear_warning_state()
    assert await repository.get_warning_state() is None


@pytest.mark.asyncio
async def test_duplicate_confirmation_round_trips(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    job = _job(tmp_path / "staged")
    assert await repository.create_job(job)

    assert await repository.transition_job(
        job.id,
        JobState.STAGED,
        JobState.FAILED,
        duplicate_confirmed=True,
    )

    loaded = await repository.get_job(job.id)
    assert loaded is not None
    assert loaded.duplicate_confirmed


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
async def test_legacy_review_finalization_gate_blocks_cleanup_until_notified(
    tmp_path: Path,
) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    job = _job(tmp_path / "staged")
    assert await repository.create_job(job)
    assert await repository.transition_job(job.id, JobState.STAGED, JobState.SUBMITTING)
    assert await repository.transition_job(
        job.id,
        JobState.SUBMITTING,
        JobState.SUBMITTED,
        task_id=uuid4(),
    )
    assert await repository.transition_job(
        job.id,
        JobState.SUBMITTED,
        JobState.SUCCEEDED,
        document_id=42,
    )
    assert await repository.set_review_finalization_state(
        job.id,
        ReviewFinalizationState.PENDING_NOTIFICATION,
    )
    future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()

    assert await repository.active_succeeded_uploads() == ()
    assert await repository.cleanup_message_ids(
        context_before=future,
        succeeded_before=future,
        failed_before=future,
    ) == ((), ())

    assert await repository.set_review_finalization_state(
        job.id,
        ReviewFinalizationState.READY_FOR_CLEANUP,
    )
    assert await repository.active_succeeded_uploads() == (
        (
            (
                DiscordMessageTarget(102, 10),
                DiscordMessageTarget(500, 50),
            ),
            42,
        ),
    )


@pytest.mark.asyncio
async def test_atomic_inbox_close_rejects_disappeared_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    await repository.create_upload_batch(
        UploadBatch(100, 102, 200, 102, 30, 1),
        (
            UploadItem(
                100,
                101,
                1,
                "synthetic.pdf",
                state=UploadItemState.SUCCEEDED,
            ),
        ),
    )

    def missing_batch(source_message_id: int) -> None:
        assert source_message_id == 100

    monkeypatch.setattr(repository, "_get_upload_batch", missing_batch)
    with pytest.raises(ValueError, match="disappeared"):
        await repository.close_upload_item_if_cleanup_eligible(100, 101)


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


@pytest.mark.asyncio
async def test_upload_batch_lifecycle_cleanup_and_purge(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLiteRepository(tmp_path / "batch.sqlite3", lease_seconds=60)
    await repository.initialize()
    batch = UploadBatch(
        source_message_id=100,
        source_channel_id=102,
        summary_message_id=200,
        summary_channel_id=102,
        principal_id=30,
        total_items=2,
    )
    first = UploadItem(100, 11, 1, "one.pdf")
    second = UploadItem(
        100,
        12,
        2,
        "two.pdf",
        state=UploadItemState.FAILED,
        failure_reason="synthetic",
    )
    with pytest.raises(ValueError, match="item count"):
        await repository.create_upload_batch(batch, (first,))
    with pytest.raises(ValueError, match="ordinals"):
        await repository.create_upload_batch(
            batch,
            (replace(first, ordinal=2), second),
        )

    await repository.create_upload_batch(batch, (first, second))
    await repository.create_upload_batch(batch, (first, second))
    assert await repository.get_upload_batch(999) is None
    snapshot = await repository.get_upload_batch(100)
    assert snapshot is not None
    assert not snapshot.cleanup_ready
    assert snapshot.cleanup_targets == (
        DiscordMessageTarget(102, 200),
        DiscordMessageTarget(102, 100),
    )
    assert tuple(item.original_filename for item in snapshot.items) == (
        "one.pdf",
        "two.pdf",
    )

    job = _job(tmp_path / "batch-job", message_id=100, attachment_id=11)
    assert await repository.create_job(job)
    snapshot = await repository.update_upload_item(
        100,
        11,
        UploadItemState.PENDING,
        job_id=job.id,
        document_id=44,
        parent_message_id=300,
        parent_channel_id=102,
        thread_id=400,
        title_message_id=401,
        metadata_message_id=402,
        actions_message_id=403,
        controls_message_id=404,
        failure_reason="temporary",
    )
    assert snapshot.items[0].job_id == job.id
    assert snapshot.items[0].document_id == DocumentId(44)
    assert snapshot.items[0].parent_message_id == 300
    assert snapshot.items[0].title_message_id == 401
    assert snapshot.items[0].metadata_message_id == 402
    assert snapshot.items[0].actions_message_id == 403
    assert snapshot.items[0].controls_message_id == 404
    assert await repository.upload_item_for_job(job.id) == snapshot.items[0]
    assert await repository.upload_item_for_job(uuid4()) is None
    assert await repository.set_review_finalization_state(
        job.id,
        ReviewFinalizationState.PENDING_NOTIFICATION,
    )
    assert not await repository.set_review_finalization_state(
        uuid4(),
        ReviewFinalizationState.PENDING_NOTIFICATION,
    )
    gated = await repository.upload_item_for_job(job.id)
    assert gated is not None
    assert gated.review_finalization_state is ReviewFinalizationState.PENDING_NOTIFICATION
    assert {item.attachment_id for item in await repository.active_upload_items()} == {
        11,
        12,
    }
    with pytest.raises(ValueError, match="does not exist"):
        await repository.update_upload_item(100, 999, UploadItemState.CLOSED)

    assert await repository.transition_job(job.id, JobState.STAGED, JobState.SUBMITTING)
    linked = await repository.upload_item_for_job(job.id)
    assert linked is not None
    assert linked.state is UploadItemState.PROCESSING
    assert await repository.transition_job(
        job.id,
        JobState.SUBMITTING,
        JobState.SUBMITTED,
        task_id=uuid4(),
    )
    assert await repository.transition_job(
        job.id,
        JobState.SUBMITTED,
        JobState.SUCCEEDED,
        document_id=44,
    )
    linked = await repository.upload_item_for_job(job.id)
    assert linked is not None
    assert linked.state is UploadItemState.SUCCEEDED
    assert await repository.active_succeeded_uploads() == ()
    assert await repository.terminal_upload_cleanup_targets() == ()

    future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()
    _, scheduled = await repository.cleanup_message_ids(
        context_before=future,
        succeeded_before=future,
        failed_before=future,
    )
    assert scheduled == ()
    _, too_recent = await repository.cleanup_message_ids(
        context_before="1970-01-01T00:00:00+00:00",
        succeeded_before="1970-01-01T00:00:00+00:00",
        failed_before="1970-01-01T00:00:00+00:00",
    )
    assert too_recent == ()
    await repository.update_upload_item(100, 11, UploadItemState.CLOSED)
    resolved = await repository.update_upload_item(100, 12, UploadItemState.DISMISSED)
    assert resolved.cleanup_ready
    assert await repository.active_upload_items() == ()
    assert len(await repository.tracked_upload_items()) == 2
    pending_artifacts = await repository.resolved_upload_items_pending_cleanup()
    assert tuple(item.attachment_id for item in pending_artifacts) == (11,)
    await repository.confirm_upload_item_cleanup(
        100,
        11,
        parent_cleaned=True,
        thread_cleaned=False,
    )
    assert (await repository.resolved_upload_items_pending_cleanup())[0].parent_cleaned
    await repository.confirm_upload_item_cleanup(
        100,
        11,
        parent_cleaned=False,
        thread_cleaned=True,
    )
    cleaned_item = (await repository.tracked_upload_items())[0]
    assert cleaned_item.parent_cleaned
    assert cleaned_item.thread_cleaned
    assert await repository.resolved_upload_items_pending_cleanup() == ()
    with pytest.raises(ValueError, match="does not exist"):
        await repository.confirm_upload_item_cleanup(
            100,
            999,
            parent_cleaned=True,
            thread_cleaned=True,
        )
    assert await repository.terminal_upload_cleanup_targets() == (
        DiscordMessageTarget(102, 200),
        DiscordMessageTarget(102, 100),
    )
    _, scheduled = await repository.cleanup_message_ids(
        context_before=future,
        succeeded_before=future,
        failed_before=future,
    )
    assert scheduled == (
        DiscordMessageTarget(102, 200),
        DiscordMessageTarget(102, 100),
    )

    await repository.confirm_message_cleanup((DiscordMessageTarget(102, 200),))
    partially_cleaned = await repository.get_upload_batch(100)
    assert partially_cleaned is not None
    assert partially_cleaned.cleanup_targets == (DiscordMessageTarget(102, 100),)
    assert await repository.terminal_upload_cleanup_targets() == (DiscordMessageTarget(102, 100),)
    await repository.confirm_message_cleanup((DiscordMessageTarget(102, 100),))
    cleaned = await repository.get_upload_batch(100)
    assert cleaned is not None
    assert cleaned.cleanup_targets == ()
    _, already_cleaned = await repository.cleanup_message_ids(
        context_before=future,
        succeeded_before=future,
        failed_before=future,
    )
    assert already_cleaned == ()

    await repository.purge(
        expired_before=future,
        audit_before=future,
        succeeded_before=future,
        failed_before=future,
    )
    assert await repository.get_upload_batch(100) is None

    other = replace(batch, source_message_id=101, summary_message_id=201, total_items=1)
    await repository.create_upload_batch(
        other,
        (UploadItem(101, 13, 1, "three.pdf"),),
    )

    def missing_batch(_: int) -> None:
        return None

    monkeypatch.setattr(repository, "_get_upload_batch", missing_batch)
    with pytest.raises(ValueError, match="disappeared"):
        await repository.update_upload_item(101, 13, UploadItemState.CLOSED)


@pytest.mark.asyncio
async def test_transcript_round_trip_is_encrypted_at_rest(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    repository = _encrypted_repository(database)
    await repository.initialize()
    transcript = _transcript()

    await repository.save_transcript(transcript)

    assert await repository.get_transcript(1, 2) == transcript
    connection = sqlite3.connect(database)
    try:
        encrypted = connection.execute(
            "SELECT encrypted_turns FROM conversation_transcripts"
        ).fetchone()[0]
    finally:
        connection.close()
    assert b"question-marker" not in encrypted
    assert b"answer-marker" not in encrypted
    await repository.close()


@pytest.mark.asyncio
async def test_transcripts_are_isolated_replaced_and_clear_preserves_session(
    tmp_path: Path,
) -> None:
    repository = _encrypted_repository(tmp_path / "db.sqlite3")
    await repository.initialize()
    session = _session()
    await repository.save_search_session(session)
    await repository.save_transcript(_transcript())
    replacement = ConversationTranscript(
        1,
        2,
        (ConversationTurn("replacement-question", "replacement-answer"),),
        datetime.now(tz=UTC) + timedelta(minutes=5),
    )
    await repository.save_transcript(replacement)
    await repository.save_transcript(_transcript(guild_id=1, thread_id=3))

    assert await repository.get_transcript(1, 2) == replacement
    assert await repository.get_transcript(1, 3) is not None
    assert await repository.clear_transcript(1, 2)
    assert not await repository.clear_transcript(1, 2)
    assert await repository.get_search_session(session.id) == session
    await repository.close()


@pytest.mark.asyncio
async def test_transcript_expiry_and_corrupt_values_are_safely_absent(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    repository = _encrypted_repository(database)
    await repository.initialize()
    await repository.save_transcript(_transcript(expired=True))
    assert await repository.get_transcript(1, 2) is None
    future = (datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE conversation_transcripts SET encrypted_turns = ?, expires_at = ?",
            (b"not-a-token", future),
        )
        connection.commit()
    assert await repository.get_transcript(1, 2) is None
    assert repository._fernet is not None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE conversation_transcripts SET encrypted_turns = ?, expires_at = ?",
            (repository._fernet.encrypt(b"not-json"), future),
        )
        connection.commit()
    assert await repository.get_transcript(1, 2) is None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE conversation_transcripts SET encrypted_turns = ?, expires_at = ?",
            (
                repository._fernet.encrypt(b'{"question":"marker"}'),
                future,
            ),
        )
        connection.commit()
    assert await repository.get_transcript(1, 2) is None
    await repository.close()


@pytest.mark.asyncio
async def test_transcript_with_malformed_encrypted_turn_is_safely_absent(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    repository = _encrypted_repository(database)
    await repository.initialize()
    await repository.save_transcript(_transcript())
    assert repository._fernet is not None
    future = (datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat()
    malformed_turns = b'[{"question":"marker"}]'
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE conversation_transcripts SET encrypted_turns = ?, expires_at = ?",
            (repository._fernet.encrypt(malformed_turns), future),
        )
        connection.commit()

    assert await repository.get_transcript(1, 2) is None
    await repository.close()


@pytest.mark.asyncio
async def test_transcript_requires_configured_encryption_key(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()

    with pytest.raises(ValueError, match="encryption key"):
        await repository.save_transcript(_transcript())
    with pytest.raises(ValueError, match="encryption key"):
        await repository.get_transcript(1, 2)
    await repository.close()


@pytest.mark.asyncio
async def test_search_session_round_trip_and_query_is_not_persisted(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    repository = SQLiteRepository(database, lease_seconds=60)
    await repository.initialize()
    session = _session()

    await repository.save_search_session(session)

    assert await repository.get_search_session(session.id) == session
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(search_sessions)")}
        row = connection.execute("SELECT * FROM search_sessions").fetchone()
    assert "query" not in columns
    assert row is not None
    assert "question-marker" not in repr(tuple(row))
    await repository.close()


@pytest.mark.asyncio
async def test_search_sessions_select_latest_and_are_isolated(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    created = datetime.now(tz=UTC)
    earlier = _session(session_id=UUID(int=1), created_at=created - timedelta(seconds=1))
    latest = _session(session_id=UUID(int=2), created_at=created)
    other = _session(guild_id=9, thread_id=9)
    await repository.save_search_session(earlier)
    await repository.save_search_session(latest)
    await repository.save_search_session(other)

    assert await repository.latest_search_session(1, 2) == latest
    assert await repository.latest_search_session(9, 9) == other
    assert await repository.latest_search_session(1, 9) is None
    await repository.close()


@pytest.mark.asyncio
async def test_search_session_page_update_rejects_invalid_and_expired_state(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "db.sqlite3", lease_seconds=60)
    await repository.initialize()
    session = _session()
    await repository.save_search_session(session)

    updated = await repository.set_search_session_page(session.id, 2)

    assert updated is not None
    assert updated.page == 2
    with pytest.raises(ValueError, match="non-negative"):
        await repository.set_search_session_page(session.id, -1)
    assert await repository.set_search_session_page(uuid4(), 0) is None
    expired = _session(expired=True)
    await repository.save_search_session(expired)
    assert await repository.get_search_session(expired.id) is None
    assert await repository.set_search_session_page(expired.id, 0) is None
    await repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statement", "value"),
    [
        ("UPDATE search_sessions SET mode = ?", "unknown"),
        ("UPDATE search_sessions SET document_ids_json = ?", "not-json"),
        ("UPDATE search_sessions SET card_message_ids_json = ?", "[true]"),
        (
            "UPDATE search_sessions SET cleanup_targets_json = ?",
            '[{"channel_id":true,"message_id":2}]',
        ),
        ("UPDATE search_sessions SET navigation_message_id = ?", "not-an-id"),
    ],
)
async def test_malformed_search_session_row_is_safely_absent(
    tmp_path: Path, statement: str, value: object
) -> None:
    database = tmp_path / "db.sqlite3"
    repository = SQLiteRepository(database, lease_seconds=60)
    await repository.initialize()
    session = _session()
    await repository.save_search_session(session)
    with sqlite3.connect(database) as connection:
        connection.execute(statement, (value,))
        connection.commit()

    assert await repository.get_search_session(session.id) is None
    await repository.close()


@pytest.mark.asyncio
async def test_initialize_expires_legacy_reference_context_without_migration(
    tmp_path: Path,
) -> None:
    database = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO reference_context VALUES (?, ?, ?, ?)",
            (1, "[1]", "[2]", (datetime.now(tz=UTC) + timedelta(minutes=5)).isoformat()),
        )
        connection.commit()
    finally:
        connection.close()
    repository = SQLiteRepository(database, lease_seconds=60)

    await repository.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reference_context").fetchone()[0] == 0
    await repository.close()


@pytest.mark.asyncio
async def test_purge_removes_expired_transcripts_sessions_and_registers_cleanup_targets(
    tmp_path: Path,
) -> None:
    repository = _encrypted_repository(tmp_path / "db.sqlite3")
    await repository.initialize()
    await repository.save_transcript(_transcript(expired=True))
    session = _session(expired=True)
    await repository.save_search_session(session)
    future = (datetime.now(tz=UTC) + timedelta(days=1)).isoformat()

    await repository.purge(
        expired_before=future,
        audit_before=future,
        succeeded_before=future,
        failed_before=future,
    )

    assert await repository.get_transcript(1, 2) is None
    assert await repository.get_search_session(session.id) is None
    question_targets, _ = await repository.cleanup_message_ids(
        context_before=future, succeeded_before=future, failed_before=future
    )
    assert set(question_targets) == set(session.cleanup_targets)
    await repository.close()
