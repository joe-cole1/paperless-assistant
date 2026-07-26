"""Application-service tests with synthetic Paperless boundaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    AmbiguousSubmissionError,
    ConfigurationUnavailableError,
    PaperlessUnavailableError,
    RateLimitedError,
)
from paperless_assistant.models import (
    ChatResult,
    Document,
    DocumentId,
    Download,
    IngestionJob,
    JobState,
    MetadataGuidance,
    PaperlessTask,
    TaskState,
    Taxonomy,
    TaxonomyItem,
)
from paperless_assistant.repository import SQLiteRepository
from paperless_assistant.services import (
    DeliveryService,
    IngestionOutcome,
    IngestionService,
    QueryService,
    QuestionRateLimiter,
    TaxonomyCache,
)


class FakeGateway:
    """Controllable synthetic implementation of the Paperless port."""

    def __init__(self) -> None:
        self.chat_result = ChatResult("Native answer", (DocumentId(7),))
        self.chat_error = False
        self.search = (Document(DocumentId(8), "Search result", date(2024, 1, 1)),)
        self.documents = {
            7: Document(DocumentId(7), "Native result", date(2024, 2, 2)),
            8: self.search[0],
            44: Document(DocumentId(44), "Consumed", date(2024, 3, 3)),
        }
        self.taxonomy = Taxonomy((TaxonomyItem(1, "Discord"),), (), ())
        self.taxonomy_error = False
        self.submit_error: Exception | None = None
        self.task_id = uuid4()
        self.task = PaperlessTask(self.task_id, TaskState.SUCCESS, DocumentId(44))
        self.note_error = False
        self.notes: list[str] = []
        self.download_sizes = {"original": 4, "archived": 3}
        self.download_error_archived = False
        self.last_question: tuple[str, int | None] | None = None

    async def chat(self, question: str, document_id: int | None = None) -> ChatResult:
        self.last_question = (question, document_id)
        if self.chat_error:
            raise PaperlessUnavailableError("synthetic")
        return self.chat_result

    async def search_documents(self, query: str, limit: int = 3) -> tuple[Document, ...]:
        assert query
        return self.search[:limit]

    async def get_document(self, document_id: int) -> Document:
        return self.documents[document_id]

    async def get_taxonomy(self) -> Taxonomy:
        if self.taxonomy_error:
            raise PaperlessUnavailableError("synthetic")
        return self.taxonomy

    async def submit_document(
        self,
        path: Path,
        filename: str,
        media_type: str,
        guidance: MetadataGuidance,
    ) -> UUID:
        assert path.exists()  # noqa: ASYNC240
        assert filename
        assert media_type
        assert guidance.tag_ids
        if self.submit_error:
            raise self.submit_error
        return self.task_id

    async def get_task(self, task_id: UUID) -> PaperlessTask:
        assert task_id == self.task_id
        return self.task

    async def add_note(self, document_id: int, note: str) -> None:
        assert document_id == 44
        if self.note_error:
            raise PaperlessUnavailableError("synthetic")
        self.notes.append(note)

    async def download(
        self, document_id: int, destination: Path, *, archived: bool = False
    ) -> Download:
        if archived and self.download_error_archived:
            raise PaperlessUnavailableError("synthetic")
        kind = "archived" if archived else "original"
        content = b"x" * self.download_sizes[kind]
        destination.write_bytes(content)  # noqa: ASYNC240
        return Download(
            destination,
            f"{kind}-{document_id}.pdf",
            "application/pdf",
            len(content),
        )

    def document_url(self, document_id: int) -> str:
        return f"https://paperless.example.test/documents/{document_id}/details"

    def original_download_url(self, document_id: int) -> str:
        return f"https://paperless.example.test/download/{document_id}?original=true"


async def _services(
    settings: Settings,
    gateway: FakeGateway,
) -> tuple[SQLiteRepository, TaxonomyCache, IngestionService]:
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()
    taxonomy = TaxonomyCache(settings, gateway)
    await taxonomy.refresh()
    ingestion = IngestionService(settings, gateway, repository, repository, taxonomy)
    return repository, taxonomy, ingestion


@pytest.mark.asyncio
async def test_query_native_context_and_search_fallback(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()
    gateway = FakeGateway()
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()
    query = QueryService(settings, gateway, repository, repository)

    response = await query.ask(201, "unchanged", document_id=7)
    context = await query.context(201)
    assert response.answer == "Native answer"
    assert response.documents[0].id == DocumentId(7)
    assert gateway.last_question == ("unchanged", 7)
    assert context is not None
    assert context.source_message_ids == ()
    await query.save_rendered_context(201, (DocumentId(7),), (100,))
    context = await query.context(201)
    assert context is not None
    assert context.source_message_ids == (100,)

    gateway.chat_result = ChatResult("", ())
    fallback = await query.ask(202, "original full text")
    assert fallback.used_search_fallback
    assert fallback.documents[0].id == DocumentId(8)

    gateway.chat_error = True
    fallback = await query.ask(203, "search after failure")
    assert fallback.used_search_fallback
    assert [action async for action in repository.actions()] == [
        "question",
        "question",
        "question",
    ]

    gateway.chat_error = False
    gateway.chat_result = ChatResult("No references", ())
    empty = await query.ask(204, "answer without references")
    assert empty.documents == ()
    assert await query.context(204) is None


@pytest.mark.asyncio
async def test_question_rate_limiter() -> None:
    limiter = QuestionRateLimiter(2, timedelta(minutes=5))
    now = datetime.now(tz=UTC)
    await limiter.acquire(1, now)
    await limiter.acquire(1, now)
    with pytest.raises(RateLimitedError):
        await limiter.acquire(1, now)
    await limiter.acquire(1, now + timedelta(minutes=6))
    await limiter.acquire(2, now)


@pytest.mark.asyncio
async def test_taxonomy_readiness_and_guidance(
    settings_factory: Callable[..., Settings],
) -> None:
    gateway = FakeGateway()
    cache = TaxonomyCache(settings_factory(), gateway)

    assert await cache.refresh()
    assert cache.ingestion_ready
    assert cache.guidance("unmatched").tag_ids == (1,)
    gateway.taxonomy = Taxonomy((TaxonomyItem(1, "Discord"), TaxonomyItem(2, "discord")), (), ())
    assert not await cache.refresh()
    assert not cache.ingestion_ready
    gateway.taxonomy_error = True
    assert not await cache.refresh()
    with pytest.raises(ConfigurationUnavailableError):
        cache.guidance("anything")


@pytest.mark.asyncio
async def test_ingestion_success_note_and_duplicate_event(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    repository, _, ingestion = await _services(settings, gateway)
    staged = settings.staging_dir / "one"
    staged.write_bytes(b"%PDF-1.7")

    job = await ingestion.stage(
        discord_message_id=10,
        discord_attachment_id=20,
        principal_id=201,
        staged_path=staged,
        original_filename="synthetic.pdf",
        caption="vaccine guidance",
    )
    assert job is not None
    duplicate = await ingestion.stage(
        discord_message_id=10,
        discord_attachment_id=20,
        principal_id=201,
        staged_path=staged,
        original_filename="synthetic.pdf",
        caption="vaccine guidance",
    )
    assert duplicate is None
    submitted = await ingestion.submit(job)
    assert submitted.job.state == JobState.SUBMITTED
    repeated = await ingestion.submit(job)
    assert repeated.job.state == JobState.SUBMITTED
    completed = await ingestion.poll_once(submitted.job)
    assert completed.job.state == JobState.SUCCEEDED
    assert completed.document is not None
    assert gateway.notes == ["Discord upload guidance: vaccine guidance"]
    assert not staged.exists()
    assert await ingestion.message_succeeded(10)
    assert not await ingestion.message_succeeded(999)
    warning_time = datetime.now(tz=UTC)
    assert await ingestion.warning_state() is None
    await ingestion.record_warning(90, warning_time)
    assert await ingestion.warning_state() == (90, warning_time)
    await ingestion.clear_warning()
    assert await ingestion.warning_state() is None
    assert [action async for action in repository.actions()] == [
        "ingestion",
        "ingestion",
        "ingestion",
        "ingestion",
    ]
    assert (await ingestion.poll_once(completed.job)).job.state == JobState.SUCCEEDED
    assert (await ingestion.poll_until_notifiable(completed.job)).job.state == JobState.SUCCEEDED


@pytest.mark.asyncio
async def test_ingestion_failures_and_recovery(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    repository, _, ingestion = await _services(settings, gateway)

    async def make_job(number: int) -> IngestionJob:
        path = settings.staging_dir / str(number)
        path.write_bytes(b"%PDF-1.7")
        job = await ingestion.stage(
            discord_message_id=number,
            discord_attachment_id=number,
            principal_id=201,
            staged_path=path,
            original_filename="synthetic.pdf",
            caption="",
        )
        assert job is not None
        return job

    ambiguous = await make_job(1)
    gateway.submit_error = AmbiguousSubmissionError("synthetic")
    outcome = await ingestion.submit(ambiguous)
    assert outcome.job.state == JobState.RECONCILIATION_REQUIRED

    rejected = await make_job(2)
    gateway.submit_error = PaperlessUnavailableError("synthetic")
    outcome = await ingestion.submit(rejected)
    assert outcome.job.state == JobState.FAILED
    assert not rejected.staged_path.exists()

    failed_task = await make_job(3)
    gateway.submit_error = None
    submitted = await ingestion.submit(failed_task)
    gateway.task = PaperlessTask(gateway.task_id, TaskState.PENDING)
    assert (await ingestion.poll_once(submitted.job)).job.state == JobState.SUBMITTED
    gateway.task = PaperlessTask(gateway.task_id, TaskState.FAILURE)
    outcome = await ingestion.poll_once(submitted.job)
    assert outcome.job.state == JobState.FAILED

    interrupted = await make_job(4)
    await repository.transition_job(interrupted.id, JobState.STAGED, JobState.SUBMITTING)
    notifications: list[JobState] = []

    async def notify(value: IngestionOutcome) -> None:
        notifications.append(value.job.state)

    await ingestion.recover(notify)
    loaded = await repository.get_job(interrupted.id)
    assert loaded is not None
    assert loaded.state == JobState.RECONCILIATION_REQUIRED
    assert JobState.RECONCILIATION_REQUIRED in notifications


@pytest.mark.asyncio
async def test_ingestion_invariants_polling_and_recovery_paths(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "data",
        paperless_native_task_notification_timeout_seconds=30,
        paperless_task_poll_initial_seconds=0.01,
    )
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    repository, _, ingestion = await _services(settings, gateway)

    missing = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=1,
        principal_id=201,
        staged_path=settings.staging_dir / "missing",
        original_filename="synthetic.pdf",
        media_type="application/pdf",
        office_dependent=False,
        caption="",
        guidance=MetadataGuidance((1,), None, None),
    )
    with pytest.raises(RuntimeError, match="disappeared"):
        await ingestion.submit(missing)
    with pytest.raises(RuntimeError, match="disappeared"):
        await ingestion._required_job(uuid4())

    async def stage(number: int, caption: str = "") -> IngestionJob:
        path = settings.staging_dir / str(number)
        path.write_bytes(b"%PDF-1.7")
        job = await ingestion.stage(
            discord_message_id=number,
            discord_attachment_id=number,
            principal_id=201,
            staged_path=path,
            original_filename="synthetic.pdf",
            caption=caption,
        )
        assert job is not None
        return job

    pending = await ingestion.submit(await stage(2))
    gateway.task = PaperlessTask(gateway.task_id, TaskState.PENDING)
    ingestion._settings = settings.model_copy(
        update={"paperless_native_task_notification_timeout_seconds": 0}
    )
    timed_out = await ingestion.poll_until_notifiable(pending.job)
    assert timed_out.notification_timed_out

    completed = await ingestion.submit(await stage(3))
    tasks = iter(
        (
            PaperlessTask(gateway.task_id, TaskState.PENDING),
            PaperlessTask(gateway.task_id, TaskState.SUCCESS, DocumentId(44)),
        )
    )

    async def next_task(_: UUID) -> PaperlessTask:
        return next(tasks)

    monkeypatch.setattr(gateway, "get_task", next_task)
    sleep = AsyncMock(return_value=None)
    monkeypatch.setattr("paperless_assistant.services.asyncio.sleep", sleep)
    ingestion._settings = settings.model_copy(
        update={"paperless_native_task_notification_timeout_seconds": 30}
    )
    outcome = await ingestion.poll_until_notifiable(completed.job)
    assert outcome.job.state == JobState.SUCCEEDED
    assert outcome.document is not None
    assert not gateway.notes
    sleep.assert_awaited_once()

    staged = await stage(4)
    submitted = await ingestion.submit(await stage(5))
    gateway.get_task = AsyncMock(side_effect=PaperlessUnavailableError("synthetic"))  # type: ignore[method-assign]
    await ingestion.recover()
    loaded_staged = await repository.get_job(staged.id)
    loaded_submitted = await repository.get_job(submitted.job.id)
    assert loaded_staged is not None
    assert loaded_staged.state == JobState.SUBMITTED
    assert loaded_submitted is not None
    assert loaded_submitted.state == JobState.SUBMITTED


@pytest.mark.asyncio
async def test_note_failure_does_not_roll_back(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    _, _, ingestion = await _services(settings, gateway)
    path = settings.staging_dir / "note"
    path.write_bytes(b"%PDF-1.7")
    job = await ingestion.stage(
        discord_message_id=50,
        discord_attachment_id=50,
        principal_id=201,
        staged_path=path,
        original_filename="synthetic.pdf",
        caption="private",
    )
    assert job is not None
    submitted = await ingestion.submit(job)
    gateway.note_error = True
    outcome = await ingestion.poll_once(submitted.job)

    assert outcome.job.state == JobState.SUCCEEDED
    assert outcome.note_failed


@pytest.mark.asyncio
async def test_delivery_original_archive_and_link(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "data",
        delivery_min_free_bytes=0,
    )
    settings.delivery_dir.mkdir(parents=True)
    gateway = FakeGateway()
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()
    delivery = DeliveryService(settings, gateway, repository)

    original = await delivery.prepare(201, 7, 10)
    assert original.attachment is not None
    assert not original.used_archived
    delivery.cleanup(original)
    assert not original.attachment.path.exists()

    gateway.download_sizes["original"] = 20
    archived = await delivery.prepare(201, 7, 10)
    assert archived.attachment is not None
    assert archived.used_archived
    delivery.cleanup(archived)

    gateway.download_sizes["archived"] = 20
    link = await delivery.prepare(201, 7, 10)
    assert link.attachment is None
    assert "original=true" in link.original_url
    delivery.cleanup(link)

    gateway.download_error_archived = True
    link = await delivery.prepare(201, 7, 10)
    assert link.attachment is None

    constrained = settings_factory(
        data_dir=tmp_path / "constrained",
        delivery_min_free_bytes=10**18,
    )
    constrained.delivery_dir.mkdir(parents=True)
    low_disk_repository = SQLiteRepository(constrained.database_path, lease_seconds=60)
    await low_disk_repository.initialize()
    low_disk = DeliveryService(constrained, gateway, low_disk_repository)
    assert (await low_disk.prepare(201, 7, 10)).attachment is None
