"""Application-service tests with synthetic Paperless boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    AmbiguousPaperlessMutationError,
    AmbiguousSubmissionError,
    ConfigurationUnavailableError,
    DuplicateUploadError,
    PaperlessAuthenticationError,
    PaperlessPermissionError,
    PaperlessUnavailableError,
    RateLimitedError,
    StaleSuggestionError,
    UnlinkedUserError,
)
from paperless_assistant.models import (
    AISuggestions,
    ChatResult,
    DiscordMessageTarget,
    Document,
    DocumentId,
    DocumentUpdate,
    Download,
    IngestionJob,
    JobState,
    MetadataGuidance,
    PaperlessTask,
    ReviewFinalizationState,
    SuggestedDate,
    SuggestionApplyResult,
    SuggestionReview,
    TaskState,
    Taxonomy,
    TaxonomyCapabilities,
    TaxonomyItem,
    TaxonomyKind,
    UploadBatch,
    UploadItem,
    UploadItemState,
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
        self.similar: tuple[Document, ...] = (
            Document(DocumentId(9), "Similar result", date(2024, 1, 2)),
        )
        self.last_similar: tuple[int, int, object] | None = None
        self.documents = {
            7: Document(
                DocumentId(7),
                "Native result",
                date(2024, 2, 2),
                modified=datetime(2026, 7, 28, tzinfo=UTC),
                tag_ids=(1, 9),
            ),
            8: self.search[0],
            44: Document(DocumentId(44), "Consumed", date(2024, 3, 3)),
        }
        self.taxonomy = Taxonomy(
            (TaxonomyItem(1, "Discord"), TaxonomyItem(10, "inbox")),
            (),
            (),
        )
        self.taxonomy_error = False
        self.submit_error: Exception | None = None
        self.search_error = False
        self.task = PaperlessTask(uuid4(), TaskState.SUCCESS, DocumentId(44))
        self.task_id = self.task.task_id
        self.task_error = False
        self.suggestions_error = False
        self.suggestions_result = AISuggestions(
            title="Suggested",
            correspondent_ids=(1,),
            tag_ids=(2,),
        )
        self.updates_applied: DocumentUpdate | None = None
        self.skip_update = False
        self.tag_changes: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
        self.tag_mutation_error: Exception | None = None
        self.ignore_tag_changes = False
        self.document_tokens: list[object] = []
        self.taxonomy_tokens: list[object] = []
        self.tag_tokens: list[object] = []
        self.capabilities = TaxonomyCapabilities(True, True, True, True)
        self.taxonomy_matches: tuple[TaxonomyItem, ...] = ()
        self.created_taxonomy: list[tuple[TaxonomyKind, str, str | None]] = []
        self.note_error = False
        self.notes: list[str] = []
        self.download_sizes = {"original": 4, "archived": 3}
        self.download_error_archived = False
        self.last_question: tuple[str, int | None] | None = None
        self.doc_tags: dict[int, tuple[int, ...] | None] = {}
        self.batch_tags_error = False
        self.batch_tag_calls: list[tuple[int, ...]] = []

    async def validate_token(self, token: object) -> bool:
        return True

    async def get_ai_suggestions(self, document_id: int, *, token: object = None) -> AISuggestions:
        if self.suggestions_error:
            raise PaperlessUnavailableError("suggestions error")
        return self.suggestions_result

    async def update_document(
        self, document_id: int, updates: DocumentUpdate, *, token: object = None
    ) -> None:
        self.updates_applied = updates
        if self.skip_update:
            return
        current = self.documents[document_id]
        self.documents[document_id] = replace(
            current,
            title=updates.title if updates.title is not None else current.title,
            created=updates.created if updates.created is not None else current.created,
            correspondent_id=(
                updates.correspondent_id
                if updates.correspondent_id is not None
                else current.correspondent_id
            ),
            document_type_id=(
                updates.document_type_id
                if updates.document_type_id is not None
                else current.document_type_id
            ),
            storage_path_id=(
                updates.storage_path_id
                if updates.storage_path_id is not None
                else current.storage_path_id
            ),
        )

    async def modify_document_tags(
        self,
        document_id: int,
        *,
        add_tag_ids: tuple[int, ...],
        remove_tag_ids: tuple[int, ...] = (),
        token: object = None,
    ) -> None:
        if not add_tag_ids and not remove_tag_ids:
            return
        self.tag_tokens.append(token)
        if self.tag_mutation_error is not None:
            raise self.tag_mutation_error
        self.tag_changes.append((add_tag_ids, remove_tag_ids))
        if self.ignore_tag_changes:
            return
        current = self.documents[document_id]
        retained = tuple(value for value in current.tag_ids if value not in remove_tag_ids)
        self.documents[document_id] = replace(
            current,
            tag_ids=tuple(dict.fromkeys((*retained, *add_tag_ids))),
        )

    async def get_taxonomy_capabilities(self, *, token: object = None) -> TaxonomyCapabilities:
        return self.capabilities

    async def find_taxonomy_items(
        self,
        kind: TaxonomyKind,
        name: str,
        *,
        token: object = None,
    ) -> tuple[TaxonomyItem, ...]:
        return self.taxonomy_matches

    async def create_taxonomy_item(
        self,
        kind: TaxonomyKind,
        name: str,
        *,
        storage_path: str | None = None,
        token: object = None,
    ) -> TaxonomyItem:
        self.created_taxonomy.append((kind, name, storage_path))
        return TaxonomyItem(100 + len(self.created_taxonomy), name)

    async def get_document_tag_ids(
        self, document_id: int, *, token: object = None
    ) -> tuple[int, ...] | None:
        return self.doc_tags.get(document_id, (1,))

    async def get_documents_tag_ids(
        self,
        document_ids: tuple[int, ...],
        *,
        token: object = None,
    ) -> dict[int, tuple[int, ...] | None]:
        self.batch_tag_calls.append(document_ids)
        if self.batch_tags_error:
            raise PaperlessUnavailableError("synthetic batch failure")
        return {identifier: self.doc_tags.get(identifier, (1,)) for identifier in document_ids}

    async def chat(
        self, question: str, document_id: int | None = None, *, token: object = None
    ) -> ChatResult:
        self.last_question = (question, document_id)
        if self.chat_error:
            raise PaperlessUnavailableError("synthetic")
        return self.chat_result

    async def search_documents(
        self, query: str, limit: int = 3, *, token: object = None
    ) -> tuple[Document, ...]:
        assert query
        return self.search[:limit]

    async def find_similar_documents(
        self,
        document_id: int,
        limit: int = 3,
        *,
        token: object = None,
    ) -> tuple[Document, ...]:
        self.last_similar = (document_id, limit, token)
        return self.similar[:limit]

    async def get_document(self, document_id: int, *, token: object = None) -> Document:
        self.document_tokens.append(token)
        return self.documents[document_id]

    async def get_taxonomy(self, *, token: object = None) -> Taxonomy:
        self.taxonomy_tokens.append(token)
        if self.taxonomy_error:
            raise PaperlessUnavailableError("synthetic")
        return self.taxonomy

    async def submit_document(
        self,
        path: Path,
        filename: str,
        media_type: str,
        guidance: MetadataGuidance,
        *,
        token: object = None,
    ) -> UUID:
        assert path.exists()  # noqa: ASYNC240
        assert filename
        assert media_type
        assert guidance.tag_ids
        if self.submit_error:
            raise self.submit_error
        return self.task_id

    async def get_task(self, task_id: UUID, *, token: object = None) -> PaperlessTask:
        assert task_id == self.task_id
        return self.task

    async def add_note(self, document_id: int, note: str, *, token: object = None) -> None:
        assert document_id == 44
        if self.note_error:
            raise PaperlessUnavailableError("synthetic")
        self.notes.append(note)

    async def download(
        self,
        document_id: int,
        destination: Path,
        *,
        archived: bool = False,
        token: object = None,
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


_LINKED_USER_TOKEN = SecretStr("linked-user-token")


class _Credentials:
    def __init__(
        self,
        token: SecretStr | None = _LINKED_USER_TOKEN,
    ) -> None:
        self.token = token

    async def get_user_token(self, principal_id: int) -> SecretStr | None:
        assert principal_id == 201
        return self.token

    async def save_user_token(self, principal_id: int, token: SecretStr) -> None:
        assert principal_id == 201
        self.token = token

    async def delete_user_token(self, principal_id: int) -> bool:
        assert principal_id == 201
        existed = self.token is not None
        self.token = None
        return existed


def _review_job() -> IngestionJob:
    return IngestionJob(
        id=uuid4(),
        discord_message_id=901,
        discord_attachment_id=902,
        principal_id=201,
        staged_path=Path("synthetic.pdf"),
        original_filename="synthetic.pdf",
        media_type="application/pdf",
        office_dependent=False,
        caption="",
        guidance=MetadataGuidance(),
        state=JobState.SUCCEEDED,
        paperless_document_id=DocumentId(7),
    )


async def _finalization_services(
    settings: Settings,
    gateway: FakeGateway,
    *,
    credentials: _Credentials | None = None,
) -> tuple[SQLiteRepository, IngestionService, IngestionJob]:
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()
    taxonomy = TaxonomyCache(settings, gateway)
    await taxonomy.refresh()
    job = _review_job()
    await repository.create_job(job)
    await repository.create_upload_batch(
        UploadBatch(901, 102, 903, 102, 201, 1),
        (
            UploadItem(
                901,
                902,
                1,
                "synthetic.pdf",
                state=UploadItemState.SUCCEEDED,
                job_id=job.id,
                document_id=DocumentId(7),
            ),
        ),
    )
    ingestion = IngestionService(
        settings,
        gateway,
        repository,
        repository,
        taxonomy,
        credentials=credentials or _Credentials(),
    )
    return repository, ingestion, job


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
async def test_query_similar_uses_linked_token_context_and_audit(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()
    gateway = FakeGateway()
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()

    class FakeCredentials:
        async def get_user_token(self, principal_id: int) -> str | None:
            assert principal_id == 201
            return "linked-user-token"

    query = QueryService(
        settings,
        gateway,
        repository,
        repository,
        credentials=FakeCredentials(),  # type: ignore[arg-type]
    )

    response = await query.find_similar(201, 7, context_id=5001)
    context = await query.context(5001)

    assert response.documents == gateway.similar
    assert response.answer == "Documents similar to Paperless document #7:"
    assert gateway.last_similar == (7, 3, "linked-user-token")
    assert context is not None
    assert context.document_ids == (DocumentId(9),)
    assert [action async for action in repository.actions()] == ["similar_search"]


@pytest.mark.asyncio
async def test_query_similar_empty(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()
    gateway = FakeGateway()
    gateway.similar = ()
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()
    query = QueryService(settings, gateway, repository, repository)

    response = await query.find_similar(201, 7)

    assert response.documents == ()
    assert response.answer == "No similar documents were found for Paperless document #7."
    assert await query.context(201) is None


@pytest.mark.asyncio
async def test_query_similar_rejects_unlinked_user(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory()
    gateway = FakeGateway()
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()

    class MissingCredentials:
        async def get_user_token(self, principal_id: int) -> None:
            assert principal_id == 201

    linked_query = QueryService(
        settings,
        gateway,
        repository,
        repository,
        credentials=MissingCredentials(),  # type: ignore[arg-type]
    )

    with pytest.raises(UnlinkedUserError):
        await linked_query.find_similar(201, 7)


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
async def test_immediate_duplicate_is_durable_for_recovery(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    _, _, ingestion = await _services(settings, gateway)
    await ingestion.create_upload_batch(
        UploadBatch(10, 102, 30, 102, 201, 1),
        (UploadItem(10, 20, 1, "synthetic.pdf"),),
    )
    staged = settings.staging_dir / "duplicate"
    staged.write_bytes(b"%PDF-1.7")
    job = await ingestion.stage(
        discord_message_id=10,
        discord_attachment_id=20,
        principal_id=201,
        staged_path=staged,
        original_filename="synthetic.pdf",
        caption="",
    )
    assert job is not None
    gateway.submit_error = DuplicateUploadError("private upstream title")

    outcome = await ingestion.submit(job)
    recovered = await ingestion.active_upload_outcomes()

    assert outcome.job.state is JobState.FAILED
    assert outcome.job.duplicate_confirmed
    assert not staged.exists()
    assert recovered == (IngestionOutcome(outcome.job),)


@pytest.mark.asyncio
async def test_task_duplicate_does_not_expose_upstream_message(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    _, _, ingestion = await _services(settings, gateway)
    staged = settings.staging_dir / "duplicate-task"
    staged.write_bytes(b"%PDF-1.7")
    job = await ingestion.stage(
        discord_message_id=10,
        discord_attachment_id=20,
        principal_id=201,
        staged_path=staged,
        original_filename="synthetic.pdf",
        caption="",
    )
    assert job is not None
    submitted = await ingestion.submit(job)
    sensitive = "private upstream title and document 91"
    gateway.task = PaperlessTask(
        gateway.task_id,
        TaskState.FAILURE,
        message=sensitive,
        duplicate_confirmed=True,
    )

    outcome = await ingestion.poll_once(submitted.job)

    assert outcome.job.duplicate_confirmed
    assert sensitive not in repr(outcome)


@pytest.mark.asyncio
async def test_ingestion_failures_and_recovery(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
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
    assert not outcome.job.duplicate_confirmed
    assert not rejected.staged_path.exists()

    failed_task = await make_job(3)
    gateway.submit_error = None
    submitted = await ingestion.submit(failed_task)
    gateway.task = PaperlessTask(gateway.task_id, TaskState.PENDING)
    assert (await ingestion.poll_once(submitted.job)).job.state == JobState.SUBMITTED
    gateway.task = PaperlessTask(gateway.task_id, TaskState.FAILURE, message="duplicate in trash")
    outcome = await ingestion.poll_once(submitted.job)
    assert outcome.job.state == JobState.FAILED
    assert not outcome.job.duplicate_confirmed
    assert "paperless_task_failed" in caplog.messages
    diagnostic = next(
        record for record in caplog.records if record.message == "paperless_task_failed"
    )
    assert diagnostic.__dict__["paperless_error"] == "duplicate in trash"

    silent_failure = await ingestion.submit(await make_job(5))
    gateway.task = PaperlessTask(gateway.task_id, TaskState.FAILURE)
    assert (await ingestion.poll_once(silent_failure.job)).job.state == JobState.FAILED

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
async def test_ingestion_invariants_polling_and_recovery_paths(  # noqa: PLR0915
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

    async def next_task(_: UUID, *, token: object = None) -> PaperlessTask:
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

    staged_unlinked = await stage(6)

    class MissingCreds:
        async def get_user_token(self, user_id: int) -> str | None:
            return None

    ingestion._credentials = MissingCreds()  # type: ignore[assignment]
    original_submit = ingestion.submit
    ingestion.submit = AsyncMock(side_effect=UnlinkedUserError("synthetic"))  # type: ignore[method-assign]
    await ingestion.recover()
    ingestion.submit = original_submit  # type: ignore[method-assign]
    failed_unlinked = await repository.get_job(staged_unlinked.id)
    assert failed_unlinked is not None
    assert failed_unlinked.state == JobState.FAILED
    assert (await ingestion._fail_unlinked_job(failed_unlinked)).job.state == JobState.FAILED


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


@pytest.mark.asyncio
async def test_check_inbox_tag_removals(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "data",
        cleanup_inbox_tag="inbox",
        cleanup_inbox_tag_enabled=True,
    )
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    gateway.taxonomy = Taxonomy((TaxonomyItem(1, "Discord"), TaxonomyItem(2, "inbox")), (), ())
    repository, taxonomy, ingestion = await _services(settings, gateway)
    assert await ingestion.check_inbox_tag_removals() == ()

    path = settings.staging_dir / "doc"
    path.write_bytes(b"%PDF-1.7")
    job = await ingestion.stage(
        discord_message_id=10,
        discord_attachment_id=10,
        discord_status_message_id=50,
        discord_message_channel_id=102,
        discord_status_channel_id=500,
        principal_id=201,
        staged_path=path,
        original_filename="synthetic.pdf",
        caption="caption",
    )
    assert job is not None
    await repository.transition_job(job.id, JobState.STAGED, JobState.SUBMITTING)
    await repository.transition_job(job.id, JobState.SUBMITTING, JobState.SUBMITTED)
    await repository.transition_job(job.id, JobState.SUBMITTED, JobState.SUCCEEDED, document_id=44)

    gateway.doc_tags[44] = (1, 2)
    assert await ingestion.check_inbox_tag_removals() == ()

    gateway.doc_tags[44] = (1,)  # inbox tag (2) removed!
    assert await ingestion.check_inbox_tag_removals() == (
        DiscordMessageTarget(102, 10),
        DiscordMessageTarget(500, 50),
    )
    assert gateway.batch_tag_calls == [(44,), (44,)]

    gateway.batch_tags_error = True
    assert await ingestion.check_inbox_tag_removals() == ()
    gateway.batch_tags_error = False

    gateway.taxonomy_error = True
    assert not await taxonomy.refresh()
    assert await ingestion.check_inbox_tag_removals() == ()
    gateway.taxonomy_error = False
    assert await taxonomy.refresh()

    gateway.taxonomy = Taxonomy((TaxonomyItem(1, "Discord"),), (), ())
    assert await taxonomy.refresh()
    assert await ingestion.check_inbox_tag_removals() == ()

    disabled_settings = settings_factory(
        data_dir=tmp_path / "disabled",
        cleanup_inbox_tag_enabled=False,
    )
    _, _, disabled_ingestion = await _services(disabled_settings, gateway)
    assert await disabled_ingestion.check_inbox_tag_removals() == ()
    assert await disabled_ingestion.check_inbox_upload_closures() == ((), ())


@pytest.mark.asyncio
async def test_upload_batch_service_resolution_and_inbox_closures(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "batch-data",
        cleanup_inbox_tag="inbox",
        cleanup_inbox_tag_enabled=True,
    )
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    gateway.taxonomy = Taxonomy(
        (TaxonomyItem(1, "Discord"), TaxonomyItem(2, "inbox")),
        (),
        (),
    )
    repository, taxonomy, ingestion = await _services(settings, gateway)
    batch = UploadBatch(20, 102, 50, 102, 201, 2)
    items = (
        UploadItem(20, 21, 1, "one.pdf"),
        UploadItem(20, 22, 2, "two.pdf"),
    )
    await ingestion.create_upload_batch(batch, items)
    assert await ingestion.upload_batch(20) is not None
    assert await ingestion.terminal_upload_cleanup_targets() == ()
    with pytest.raises(ValueError, match="closed or dismissed"):
        await ingestion.resolve_upload_item(20, 21, UploadItemState.SUCCEEDED)

    jobs: list[IngestionJob] = []
    for attachment_id, document_id in ((21, 44), (22, 45)):
        staged = settings.staging_dir / str(attachment_id)
        staged.write_bytes(b"%PDF-1.7")
        job = await ingestion.stage(
            discord_message_id=20,
            discord_attachment_id=attachment_id,
            discord_status_message_id=50,
            discord_message_channel_id=102,
            discord_status_channel_id=102,
            principal_id=201,
            staged_path=staged,
            original_filename=f"{attachment_id}.pdf",
            caption="",
        )
        assert job is not None
        jobs.append(job)
        await repository.transition_job(job.id, JobState.STAGED, JobState.SUBMITTING)
        await repository.transition_job(job.id, JobState.SUBMITTING, JobState.SUBMITTED)
        await repository.transition_job(
            job.id,
            JobState.SUBMITTED,
            JobState.SUCCEEDED,
            document_id=document_id,
        )
    linked = await ingestion.upload_item_for_job(jobs[0].id)
    assert linked is not None
    assert linked.document_id == DocumentId(44)
    updated = await ingestion.update_upload_item(
        20,
        21,
        UploadItemState.SUCCEEDED,
        parent_message_id=301,
        parent_channel_id=102,
        thread_id=401,
        title_message_id=501,
        metadata_message_id=502,
        actions_message_id=503,
        controls_message_id=504,
    )
    assert updated.items[0].thread_id == 401
    assert updated.items[0].controls_message_id == 504
    assert len(await ingestion.tracked_upload_items()) == 2
    assert await ingestion.resolved_upload_items_pending_cleanup() == ()

    gateway.doc_tags = {44: (1, 2), 45: (1,)}
    closed, targets = await ingestion.check_inbox_upload_closures()
    assert tuple(item.document_id for item in closed) == (DocumentId(45),)
    assert targets == ()

    gateway.doc_tags[44] = (1,)
    closed, targets = await ingestion.check_inbox_upload_closures()
    assert tuple(item.document_id for item in closed) == (DocumentId(44),)
    assert targets == (
        DiscordMessageTarget(102, 50),
        DiscordMessageTarget(102, 20),
    )
    pending_cleanup = await ingestion.resolved_upload_items_pending_cleanup()
    assert tuple(item.attachment_id for item in pending_cleanup) == (21,)
    await ingestion.confirm_upload_item_cleanup(
        20,
        21,
        parent_cleaned=True,
        thread_cleaned=True,
    )
    assert await ingestion.resolved_upload_items_pending_cleanup() == ()
    await ingestion.confirm_upload_cleanup(targets)
    assert await ingestion.terminal_upload_cleanup_targets() == ()

    assert await ingestion.check_inbox_upload_closures() == ((), ())
    gateway.taxonomy = Taxonomy((TaxonomyItem(1, "Discord"),), (), ())
    assert await taxonomy.refresh()
    await repository.update_upload_item(20, 21, UploadItemState.SUCCEEDED)
    assert await ingestion.check_inbox_upload_closures() == ((), ())
    gateway.taxonomy = Taxonomy(
        (TaxonomyItem(1, "Discord"), TaxonomyItem(2, "inbox")),
        (),
        (),
    )
    assert await taxonomy.refresh()
    gateway.batch_tags_error = True
    assert await ingestion.check_inbox_upload_closures() == ((), ())
    assert tuple(item.attachment_id for item in await ingestion.active_upload_items()) == (21,)
    assert await ingestion.resolve_upload_item(20, 21, UploadItemState.DISMISSED) == ()


@pytest.mark.asyncio
async def test_active_upload_outcomes_restore_only_terminal_reviews(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "restore-data")
    gateway = FakeGateway()
    repository = AsyncMock()
    audit = AsyncMock()
    taxonomy = TaxonomyCache(settings, gateway)

    class FakeCreds:
        async def get_user_token(self, principal_id: int) -> str | None:
            return None if principal_id == 301 else "user-token"

    ingestion = IngestionService(
        settings,
        gateway,
        repository,
        audit,
        taxonomy,
        credentials=FakeCreds(),  # type: ignore[arg-type]
    )
    base = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=1,
        principal_id=300,
        staged_path=tmp_path / "staged",
        original_filename="synthetic.pdf",
        media_type="application/pdf",
        office_dependent=False,
        caption="",
        guidance=MetadataGuidance(),
    )
    missing_id = uuid4()
    jobs = (
        replace(base, id=uuid4(), state=JobState.SUCCEEDED),
        replace(
            base,
            id=uuid4(),
            principal_id=301,
            state=JobState.SUCCEEDED,
            paperless_document_id=DocumentId(44),
        ),
        replace(
            base,
            id=uuid4(),
            state=JobState.SUCCEEDED,
            paperless_document_id=DocumentId(98),
        ),
        replace(
            base,
            id=uuid4(),
            state=JobState.SUCCEEDED,
            paperless_document_id=DocumentId(44),
        ),
        replace(base, id=uuid4(), state=JobState.FAILED),
        replace(base, id=uuid4(), state=JobState.RECONCILIATION_REQUIRED),
        replace(base, id=uuid4(), state=JobState.SUBMITTED),
    )
    items = (
        UploadItem(1, 1, 1, "no-job.pdf"),
        UploadItem(1, 2, 2, "missing-job.pdf", job_id=missing_id),
        *(
            UploadItem(1, index, index, f"{index}.pdf", job_id=job.id)
            for index, job in enumerate(jobs, start=3)
        ),
    )
    repository.active_upload_items.return_value = items
    jobs_by_id = {job.id: job for job in jobs}
    repository.get_job.side_effect = jobs_by_id.get
    original_get_document = gateway.get_document

    async def selective_document(
        document_id: int,
        *,
        token: object = None,
    ) -> Document:
        if document_id == 98:
            raise PaperlessUnavailableError("synthetic")
        return await original_get_document(document_id, token=token)

    gateway.get_document = selective_document  # type: ignore[method-assign]
    outcomes = await ingestion.active_upload_outcomes()
    assert tuple(outcome.job.state for outcome in outcomes) == (
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.RECONCILIATION_REQUIRED,
    )
    assert outcomes[0].document is not None

    ingestion._credentials = None
    repository.active_upload_items.return_value = (
        UploadItem(2, 20, 1, "system.pdf", job_id=jobs[3].id),
    )
    assert (await ingestion.active_upload_outcomes())[0].document is not None


@pytest.mark.asyncio
async def test_get_suggestion_review(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    _, _, ingestion = await _services(settings, gateway)

    class FakeCreds:
        async def get_user_token(self, user_id: int) -> str:
            return "token"

    ingestion._credentials = FakeCreds()  # type: ignore[assignment]
    linked_path = settings.staging_dir / "linked.pdf"
    linked_path.write_bytes(b"%PDF-1.7")
    assert (
        await ingestion.stage(
            discord_message_id=90,
            discord_attachment_id=91,
            principal_id=201,
            staged_path=linked_path,
            original_filename="linked.pdf",
            caption="",
        )
        is not None
    )

    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        discord_status_message_id=3,
        principal_id=201,
        staged_path=Path("synthetic.pdf"),
        original_filename="synthetic.pdf",
        caption="",
        paperless_document_id=DocumentId(7),
        media_type="application/pdf",
        office_dependent=False,
        guidance=MetadataGuidance((), None, None),
    )

    review = await ingestion.get_suggestion_review(job)
    assert review is not None
    assert review.document == gateway.documents[7]
    assert review.suggestions.title == "Suggested"
    assert review.taxonomy == gateway.taxonomy
    assert review.capabilities == gateway.capabilities

    gateway.suggestions_error = True
    with pytest.raises(PaperlessUnavailableError):
        await ingestion.get_suggestion_review(job)

    job_no_doc = replace(job, paperless_document_id=None)
    assert await ingestion.get_suggestion_review(job_no_doc) is None

    class MissingCreds:
        async def get_user_token(self, user_id: int) -> str | None:
            return None

    ingestion._credentials = MissingCreds()  # type: ignore[assignment]
    with pytest.raises(UnlinkedUserError):
        await ingestion.get_suggestion_review(job)


@pytest.mark.asyncio
async def test_suggestion_review_rejects_concurrent_change(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    gateway = FakeGateway()
    _, _, ingestion = await _services(settings, gateway)
    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=Path("synthetic.pdf"),
        original_filename="synthetic.pdf",
        caption="",
        paperless_document_id=DocumentId(7),
        media_type="application/pdf",
        office_dependent=False,
        guidance=MetadataGuidance(),
    )
    original_get_document = gateway.get_document
    calls = 0

    async def changing_document(document_id: int, *, token: object = None) -> Document:
        nonlocal calls
        calls += 1
        document = await original_get_document(document_id, token=token)
        return (
            replace(document, modified=datetime(2026, 7, 29, tzinfo=UTC))
            if calls == 2
            else document
        )

    gateway.get_document = changing_document  # type: ignore[method-assign]
    with pytest.raises(StaleSuggestionError):
        await ingestion.get_suggestion_review(job)


def test_initial_suggestion_selection_defaults() -> None:
    document = Document(
        DocumentId(7),
        "Current",
        date(2026, 7, 1),
        modified=datetime(2026, 7, 28, tzinfo=UTC),
    )
    taxonomy = Taxonomy(
        tags=(TaxonomyItem(2, "Existing Tag"),),
        correspondents=(TaxonomyItem(3, "Existing Correspondent"),),
        document_types=(TaxonomyItem(4, "Existing Type"),),
        storage_paths=(TaxonomyItem(5, "Existing/Path"),),
    )
    review = SuggestionReview(
        document,
        AISuggestions(
            title="AI Title",
            correspondent_ids=(3,),
            document_type_ids=(4, 40),
            storage_path_ids=(5,),
            tag_ids=(2,),
            dates=(
                SuggestedDate("2026-07-27", date(2026, 7, 27)),
                SuggestedDate("invalid", None),
            ),
            suggested_tags=("New Tag",),
        ),
        taxonomy,
        TaxonomyCapabilities(),
    )

    selection = IngestionService.initial_suggestion_selection(review)

    assert selection.title == "AI Title"
    assert selection.created == date(2026, 7, 27)
    assert selection.correspondent_id == 3
    assert selection.document_type_id is None
    assert selection.storage_path_id == 5
    assert selection.tag_ids == (2,)
    assert selection.new_tags == ()
    assert not review.capabilities.can_add(TaxonomyKind.TAG)
    assert TaxonomyCapabilities(add_tags=True).can_add(TaxonomyKind.TAG)


@pytest.mark.asyncio
async def test_resolve_or_create_taxonomy_is_confirmed_and_idempotent(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    gateway = FakeGateway()
    _, _, ingestion = await _services(settings, gateway)
    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=Path("synthetic.pdf"),
        original_filename="synthetic.pdf",
        caption="",
        paperless_document_id=DocumentId(7),
        media_type="application/pdf",
        office_dependent=False,
        guidance=MetadataGuidance(),
    )

    gateway.taxonomy_matches = (TaxonomyItem(77, "Existing"),)
    existing = await ingestion.resolve_or_create_taxonomy(
        job,
        TaxonomyKind.TAG,
        "existing",
        confirm_create=False,
    )
    assert existing.id == 77
    assert gateway.created_taxonomy == []

    gateway.taxonomy_matches = (TaxonomyItem(77, "Same"), TaxonomyItem(78, "same"))
    with pytest.raises(PaperlessUnavailableError, match="ambiguous"):
        await ingestion.resolve_or_create_taxonomy(
            job,
            TaxonomyKind.TAG,
            "same",
            confirm_create=True,
        )

    gateway.taxonomy_matches = ()
    with pytest.raises(PaperlessUnavailableError, match="not confirmed"):
        await ingestion.resolve_or_create_taxonomy(
            job,
            TaxonomyKind.TAG,
            "New",
            confirm_create=False,
        )

    gateway.capabilities = TaxonomyCapabilities()
    with pytest.raises(PaperlessUnavailableError, match="not permitted"):
        await ingestion.resolve_or_create_taxonomy(
            job,
            TaxonomyKind.TAG,
            "New",
            confirm_create=True,
        )

    gateway.capabilities = TaxonomyCapabilities(add_storage_paths=True)
    created = await ingestion.resolve_or_create_taxonomy(
        job,
        TaxonomyKind.STORAGE_PATH,
        "Personal",
        confirm_create=True,
        storage_path="Personal/{created_year}",
    )
    assert created.name == "Personal"
    assert gateway.created_taxonomy == [
        (TaxonomyKind.STORAGE_PATH, "Personal", "Personal/{created_year}")
    ]

    with pytest.raises(PaperlessUnavailableError, match="document"):
        await ingestion.resolve_or_create_taxonomy(
            replace(job, paperless_document_id=None),
            TaxonomyKind.TAG,
            "New",
            confirm_create=True,
        )

    class MissingCreds:
        async def get_user_token(self, user_id: int) -> str | None:
            return None

    ingestion._credentials = MissingCreds()  # type: ignore[assignment]
    with pytest.raises(UnlinkedUserError):
        await ingestion.resolve_or_create_taxonomy(
            job,
            TaxonomyKind.TAG,
            "New",
            confirm_create=True,
        )


@pytest.mark.asyncio
async def test_apply_suggestions(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "data")
    settings.staging_dir.mkdir(parents=True)
    gateway = FakeGateway()
    repository, _, ingestion = await _services(settings, gateway)

    class FakeCreds:
        async def get_user_token(self, user_id: int) -> str:
            return "token"

    ingestion._credentials = FakeCreds()  # type: ignore[assignment]

    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        discord_status_message_id=3,
        principal_id=201,
        staged_path=Path("synthetic.pdf"),
        original_filename="synthetic.pdf",
        caption="",
        paperless_document_id=DocumentId(7),
        media_type="application/pdf",
        office_dependent=False,
        guidance=MetadataGuidance((), None, None),
    )

    await repository.create_job(job)
    updates = DocumentUpdate(title="New", tag_ids=(2,))
    expected_modified = gateway.documents[7].modified
    await ingestion.apply_suggestions(
        job,
        updates,
        expected_modified=expected_modified,
    )
    assert gateway.updates_applied is not None
    assert gateway.updates_applied.title == "New"
    assert gateway.updates_applied.tag_ids is None
    assert gateway.tag_changes == [((2,), ())]
    assert gateway.documents[7].tag_ids == (1, 9, 2)

    gateway.documents[7] = replace(
        gateway.documents[7],
        modified=datetime(2026, 7, 29, tzinfo=UTC),
    )
    with pytest.raises(StaleSuggestionError):
        await ingestion.apply_suggestions(
            job,
            updates,
            expected_modified=expected_modified,
        )

    gateway.documents[7] = replace(gateway.documents[7], modified=expected_modified)
    gateway.skip_update = True
    with pytest.raises(PaperlessUnavailableError, match="did not confirm"):
        await ingestion.apply_suggestions(
            job,
            DocumentUpdate(title="Not confirmed"),
            expected_modified=expected_modified,
        )

    # Coverage for unlinked user
    class MissingCreds:
        async def get_user_token(self, user_id: int) -> str | None:
            return None

    ingestion._credentials = MissingCreds()  # type: ignore[assignment]
    with pytest.raises(UnlinkedUserError):
        await ingestion.apply_suggestions(job, updates, expected_modified=None)

    # Coverage for no paperless_document_id
    job_no_doc = replace(job, paperless_document_id=None)
    await ingestion.apply_suggestions(
        job_no_doc,
        updates,
        expected_modified=None,
    )

    ingestion._credentials = FakeCreds()  # type: ignore[assignment]
    gateway.documents[7] = replace(gateway.documents[7], modified=None)
    with pytest.raises(StaleSuggestionError):
        await ingestion.apply_suggestions(job, updates, expected_modified=None)


@pytest.mark.asyncio
async def test_review_finalization_removes_inbox_with_linked_uploader_token(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "remove-inbox")
    gateway = FakeGateway()
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 9, 10))
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert isinstance(result, SuggestionApplyResult)
    assert result.review_tags_finalized
    assert gateway.tag_changes == [((), (10,))]
    assert gateway.documents[7].tag_ids == (1, 9)
    assert all(
        isinstance(token, SecretStr) and token.get_secret_value() == "linked-user-token"
        for token in (*gateway.document_tokens, *gateway.tag_tokens)
    )
    assert isinstance(gateway.taxonomy_tokens[-1], SecretStr)


@pytest.mark.asyncio
async def test_review_finalization_adds_optional_completion_tag(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "add-completion",
        ai_review_completion_tag="AI reviewed",
    )
    gateway = FakeGateway()
    gateway.taxonomy = replace(
        gateway.taxonomy,
        tags=(*gateway.taxonomy.tags, TaxonomyItem(11, "AI reviewed")),
    )
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert result.review_tags_finalized
    assert gateway.tag_changes == [((11,), ())]
    assert gateway.documents[7].tag_ids == (1, 9, 11)


@pytest.mark.asyncio
async def test_review_finalization_combines_add_and_remove(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "both-tags",
        ai_review_completion_tag="AI reviewed",
    )
    gateway = FakeGateway()
    gateway.taxonomy = replace(
        gateway.taxonomy,
        tags=(*gateway.taxonomy.tags, TaxonomyItem(11, "AI reviewed")),
    )
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 9, 10))
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert result.review_tags_finalized
    assert gateway.tag_changes == [((11,), (10,))]
    assert gateway.documents[7].tag_ids == (1, 9, 11)


@pytest.mark.asyncio
async def test_review_finalization_audit_omits_tag_names_and_content(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "audit-minimized",
        ai_review_completion_tag="AI reviewed",
    )
    gateway = FakeGateway()
    gateway.taxonomy = replace(
        gateway.taxonomy,
        tags=(*gateway.taxonomy.tags, TaxonomyItem(11, "AI reviewed")),
    )
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 10))
    _, ingestion, job = await _finalization_services(settings, gateway)

    await ingestion.apply_suggestions(
        job,
        DocumentUpdate(title="Private title"),
        expected_modified=gateway.documents[7].modified,
    )

    connection = sqlite3.connect(settings.database_path)
    try:
        row = connection.execute(
            """
            SELECT action, outcome
            FROM audit_events
            WHERE action = 'ai_review_tag_finalization'
            """
        ).fetchone()
    finally:
        connection.close()
    assert row == ("ai_review_tag_finalization", "finalized")
    assert "inbox" not in repr(row).casefold()
    assert "reviewed" not in repr(row).casefold()
    assert "private title" not in repr(row).casefold()


@pytest.mark.asyncio
async def test_review_finalization_already_complete_is_idempotent_and_verified(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "already-finalized",
        ai_review_completion_tag="AI reviewed",
    )
    gateway = FakeGateway()
    gateway.taxonomy = replace(
        gateway.taxonomy,
        tags=(*gateway.taxonomy.tags, TaxonomyItem(11, "AI reviewed")),
    )
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 9, 11))
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert result.review_tags_finalized
    assert gateway.tag_changes == []
    assert len(gateway.document_tokens) == 3


@pytest.mark.asyncio
async def test_review_finalization_disabled_completion_only_removes_inbox(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / "disabled-completion",
        ai_review_completion_tag=None,
    )
    gateway = FakeGateway()
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 10))
    _, ingestion, job = await _finalization_services(settings, gateway)

    await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert gateway.tag_changes == [((), (10,))]


@pytest.mark.asyncio
async def test_review_finalization_missing_configured_tag_fails_closed(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "missing-tag")
    gateway = FakeGateway()
    gateway.taxonomy = replace(
        gateway.taxonomy,
        tags=tuple(tag for tag in gateway.taxonomy.tags if tag.name != "inbox"),
    )
    repository, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(title="Confirmed"),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert "missing or ambiguous" in (result.finalization_message or "")
    assert gateway.documents[7].title == "Confirmed"
    item = await repository.upload_item_for_job(job.id)
    assert item is not None
    assert item.review_finalization_state is ReviewFinalizationState.NEEDS_RECONCILIATION


@pytest.mark.asyncio
async def test_review_finalization_ambiguous_configured_tag_fails_closed(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "ambiguous-tag")
    gateway = FakeGateway()
    gateway.taxonomy = replace(
        gateway.taxonomy,
        tags=(*gateway.taxonomy.tags, TaxonomyItem(12, "INBOX")),
    )
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert gateway.tag_changes == []


@pytest.mark.asyncio
async def test_review_finalization_permission_failure_preserves_metadata(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "permission-failure")
    gateway = FakeGateway()
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 10))
    gateway.tag_mutation_error = PaperlessPermissionError("denied")
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(title="Confirmed"),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert "saved the metadata" in (result.finalization_message or "")
    assert "denied" in (result.finalization_message or "")
    assert gateway.documents[7].title == "Confirmed"


@pytest.mark.asyncio
async def test_review_finalization_stale_document_never_mutates_tags(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "stale")
    gateway = FakeGateway()
    repository, ingestion, job = await _finalization_services(settings, gateway)

    with pytest.raises(StaleSuggestionError):
        await ingestion.apply_suggestions(
            job,
            DocumentUpdate(),
            expected_modified=datetime(2025, 1, 1, tzinfo=UTC),
        )

    assert gateway.tag_changes == []
    item = await repository.upload_item_for_job(job.id)
    assert item is not None
    assert item.review_finalization_state is ReviewFinalizationState.NOT_STARTED


@pytest.mark.asyncio
async def test_ambiguous_tag_mutation_reconciles_without_retry(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "ambiguous-reconciled")
    gateway = FakeGateway()
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 10))
    _, ingestion, job = await _finalization_services(settings, gateway)
    original_modify = gateway.modify_document_tags
    attempts = 0

    async def applied_then_ambiguous(
        document_id: int,
        *,
        add_tag_ids: tuple[int, ...],
        remove_tag_ids: tuple[int, ...] = (),
        token: object = None,
    ) -> None:
        nonlocal attempts
        if not add_tag_ids and not remove_tag_ids:
            return
        attempts += 1
        await original_modify(
            document_id,
            add_tag_ids=add_tag_ids,
            remove_tag_ids=remove_tag_ids,
            token=token,
        )
        raise AmbiguousPaperlessMutationError("unknown")

    gateway.modify_document_tags = applied_then_ambiguous  # type: ignore[method-assign]
    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert result.review_tags_finalized
    assert attempts == 1


@pytest.mark.asyncio
async def test_ambiguous_tag_mutation_requires_reconciliation_before_retry(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "ambiguous-unconfirmed")
    gateway = FakeGateway()
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 10))
    gateway.tag_mutation_error = AmbiguousPaperlessMutationError("unknown")
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert "Reconcile" in (result.finalization_message or "")
    assert len(gateway.tag_tokens) == 1


@pytest.mark.asyncio
async def test_review_finalization_verification_mismatch_fails_closed(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "verification-mismatch")
    gateway = FakeGateway()
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 10))
    gateway.ignore_tag_changes = True
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert "did not confirm" in (result.finalization_message or "")


@pytest.mark.asyncio
async def test_review_finalization_revoked_credential_never_uses_system_token(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "revoked")
    gateway = FakeGateway()
    _, ingestion, job = await _finalization_services(
        settings,
        gateway,
        credentials=_Credentials(None),
    )

    with pytest.raises(UnlinkedUserError):
        await ingestion.apply_suggestions(
            job,
            DocumentUpdate(),
            expected_modified=gateway.documents[7].modified,
        )

    assert gateway.document_tokens == []
    assert gateway.tag_tokens == []


@pytest.mark.asyncio
async def test_review_finalization_missing_credential_store_never_uses_system_token(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "missing-credential-store")
    gateway = FakeGateway()
    _, ingestion, job = await _finalization_services(settings, gateway)
    ingestion._credentials = None

    with pytest.raises(UnlinkedUserError):
        await ingestion.apply_suggestions(
            job,
            DocumentUpdate(),
            expected_modified=gateway.documents[7].modified,
        )

    assert gateway.document_tokens == []
    assert gateway.tag_tokens == []


@pytest.mark.asyncio
async def test_review_finalization_rejected_credential_reports_metadata_saved(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "rejected-after-save")
    gateway = FakeGateway()
    _, ingestion, job = await _finalization_services(settings, gateway)
    gateway.get_taxonomy = AsyncMock(  # type: ignore[method-assign]
        side_effect=PaperlessAuthenticationError("revoked")
    )

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(title="Confirmed"),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert "saved the metadata" in (result.finalization_message or "")
    assert "Relink" in (result.finalization_message or "")


@pytest.mark.asyncio
async def test_review_cleanup_gate_opens_only_after_success_notification(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "cleanup-gate")
    gateway = FakeGateway()
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 10))
    repository, ingestion, job = await _finalization_services(settings, gateway)
    gateway.doc_tags[7] = (1,)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )
    before_notification = await ingestion.check_inbox_upload_closures()
    await ingestion.mark_review_finalization_notified(job)
    after_notification = await ingestion.check_inbox_upload_closures()

    assert result is not None
    assert result.review_tags_finalized
    assert before_notification == ((), ())
    assert tuple(item.document_id for item in after_notification[0]) == (DocumentId(7),)
    assert after_notification[1] == (
        DiscordMessageTarget(102, 903),
        DiscordMessageTarget(102, 901),
    )
    item = await repository.upload_item_for_job(job.id)
    assert item is not None
    assert item.state is UploadItemState.CLOSED


@pytest.mark.asyncio
async def test_cleanup_rechecks_durable_gate_before_closing_stale_candidate(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "atomic-cleanup-gate")
    gateway = FakeGateway()
    repository, ingestion, job = await _finalization_services(settings, gateway)
    stale_candidate = await repository.upload_item_for_job(job.id)
    assert stale_candidate is not None
    assert await repository.set_review_finalization_state(
        job.id,
        ReviewFinalizationState.PENDING_NOTIFICATION,
    )
    repository.active_upload_items = AsyncMock(  # type: ignore[method-assign]
        return_value=(stale_candidate,)
    )
    gateway.doc_tags[7] = (1,)

    assert await ingestion.check_inbox_upload_closures() == ((), ())
    current = await repository.upload_item_for_job(job.id)
    assert current is not None
    assert current.state is UploadItemState.SUCCEEDED


@pytest.mark.asyncio
async def test_metadata_verification_failure_resets_cleanup_gate(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "metadata-unconfirmed")
    gateway = FakeGateway()
    gateway.skip_update = True
    repository, ingestion, job = await _finalization_services(settings, gateway)

    with pytest.raises(PaperlessUnavailableError, match="did not confirm"):
        await ingestion.apply_suggestions(
            job,
            DocumentUpdate(title="Not applied"),
            expected_modified=gateway.documents[7].modified,
        )

    item = await repository.upload_item_for_job(job.id)
    assert item is not None
    assert item.review_finalization_state is ReviewFinalizationState.NOT_STARTED


@pytest.mark.asyncio
async def test_untracked_metadata_failure_preserves_original_error(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "untracked-metadata-failure")
    gateway = FakeGateway()
    gateway.skip_update = True
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()
    taxonomy = TaxonomyCache(settings, gateway)
    await taxonomy.refresh()
    ingestion = IngestionService(
        settings,
        gateway,
        repository,
        repository,
        taxonomy,
        credentials=_Credentials(),
    )

    with pytest.raises(PaperlessUnavailableError, match="did not confirm"):
        await ingestion.apply_suggestions(
            _review_job(),
            DocumentUpdate(title="Not applied"),
            expected_modified=gateway.documents[7].modified,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message_fragment"),
    [
        (PaperlessPermissionError("denied"), "visibility and permissions"),
        (PaperlessUnavailableError("offline"), "could not load"),
    ],
)
async def test_review_finalization_taxonomy_read_failure_is_actionable(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    error: Exception,
    message_fragment: str,
) -> None:
    settings = settings_factory(data_dir=tmp_path / type(error).__name__)
    gateway = FakeGateway()
    _, ingestion, job = await _finalization_services(settings, gateway)
    gateway.get_taxonomy = AsyncMock(side_effect=error)  # type: ignore[method-assign]

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert message_fragment in (result.finalization_message or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message_fragment"),
    [
        (PaperlessAuthenticationError("revoked"), "Relink"),
        (PaperlessUnavailableError("offline"), "No automatic retry"),
    ],
)
async def test_review_finalization_definite_mutation_failure_is_actionable(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    error: Exception,
    message_fragment: str,
) -> None:
    settings = settings_factory(data_dir=tmp_path / type(error).__name__)
    gateway = FakeGateway()
    gateway.documents[7] = replace(gateway.documents[7], tag_ids=(1, 10))
    gateway.tag_mutation_error = error
    _, ingestion, job = await _finalization_services(settings, gateway)

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert message_fragment in (result.finalization_message or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message_fragment"),
    [
        (PaperlessAuthenticationError("revoked"), "Relink"),
        (PaperlessPermissionError("denied"), "document permissions"),
        (PaperlessUnavailableError("offline"), "could not be verified"),
    ],
)
async def test_review_finalization_verification_read_failure_is_actionable(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    error: Exception,
    message_fragment: str,
) -> None:
    settings = settings_factory(data_dir=tmp_path / type(error).__name__)
    gateway = FakeGateway()
    _, ingestion, job = await _finalization_services(settings, gateway)
    original_get_document = gateway.get_document
    call_count = 0

    async def fail_final_verification(
        document_id: int,
        *,
        token: object = None,
    ) -> Document:
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise error
        return await original_get_document(document_id, token=token)

    gateway.get_document = fail_final_verification  # type: ignore[method-assign]
    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert message_fragment in (result.finalization_message or "")


@pytest.mark.asyncio
async def test_untracked_review_finalization_failure_still_fails_closed(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path / "legacy-no-gate")
    gateway = FakeGateway()
    gateway.taxonomy = Taxonomy((TaxonomyItem(1, "Discord"),), (), ())
    repository = SQLiteRepository(settings.database_path, lease_seconds=60)
    await repository.initialize()
    taxonomy = TaxonomyCache(settings, gateway)
    await taxonomy.refresh()
    ingestion = IngestionService(
        settings,
        gateway,
        repository,
        repository,
        taxonomy,
        credentials=_Credentials(),
    )
    job = _review_job()

    result = await ingestion.apply_suggestions(
        job,
        DocumentUpdate(),
        expected_modified=gateway.documents[7].modified,
    )

    assert result is not None
    assert not result.review_tags_finalized
    assert await repository.upload_item_for_job(job.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_name", "updates"),
    [
        ("allow_edit_title", DocumentUpdate(title="Disabled")),
        ("allow_edit_date", DocumentUpdate(created=date(2026, 7, 30))),
        ("allow_edit_correspondent", DocumentUpdate(correspondent_id=2)),
        ("allow_edit_document_type", DocumentUpdate(document_type_id=3)),
        ("allow_edit_storage_path", DocumentUpdate(storage_path_id=4)),
        ("allow_edit_tags", DocumentUpdate(tag_ids=(5,))),
    ],
)
async def test_apply_suggestions_rejects_server_disabled_fields(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    setting_name: str,
    updates: DocumentUpdate,
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / setting_name,
        **{setting_name: False},
    )
    _, _, ingestion = await _services(settings, FakeGateway())

    class FakeCreds:
        async def get_user_token(self, user_id: int) -> str:
            return "token"

    ingestion._credentials = FakeCreds()  # type: ignore[assignment]
    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=Path("synthetic.pdf"),
        original_filename="synthetic.pdf",
        caption="",
        paperless_document_id=DocumentId(7),
        media_type="application/pdf",
        office_dependent=False,
        guidance=MetadataGuidance(),
    )

    with pytest.raises(PaperlessUnavailableError, match="field editing is disabled"):
        await ingestion.apply_suggestions(
            job,
            updates,
            expected_modified=datetime(2026, 7, 28, tzinfo=UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_name", "kind"),
    [
        ("allow_edit_tags", TaxonomyKind.TAG),
        ("allow_edit_correspondent", TaxonomyKind.CORRESPONDENT),
        ("allow_edit_document_type", TaxonomyKind.DOCUMENT_TYPE),
        ("allow_edit_storage_path", TaxonomyKind.STORAGE_PATH),
    ],
)
async def test_taxonomy_resolution_rejects_server_disabled_fields(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    setting_name: str,
    kind: TaxonomyKind,
) -> None:
    settings = settings_factory(
        data_dir=tmp_path / setting_name,
        **{setting_name: False},
    )
    _, _, ingestion = await _services(settings, FakeGateway())

    class FakeCreds:
        async def get_user_token(self, user_id: int) -> str:
            return "token"

    ingestion._credentials = FakeCreds()  # type: ignore[assignment]
    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=Path("synthetic.pdf"),
        original_filename="synthetic.pdf",
        caption="",
        paperless_document_id=DocumentId(7),
        media_type="application/pdf",
        office_dependent=False,
        guidance=MetadataGuidance(),
    )

    with pytest.raises(PaperlessUnavailableError, match="field editing is disabled"):
        await ingestion.resolve_or_create_taxonomy(
            job,
            kind,
            "Disabled",
            confirm_create=True,
        )
