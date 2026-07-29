"""Discord routing and response tests with synthetic transport objects."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import discord
import pytest
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.discord_adapter import (
    AISuggestionsDateModal,
    AISuggestionsTagModal,
    AISuggestionsTitleModal,
    AISuggestionsView,
    DiscordAssistant,
    UploadDismissButton,
    _bounded_lines,
    _close_existing_items,
    _ConfirmCloseAllView,
    _ConfirmCloseThreadView,
    _ConfirmDismissFailedView,
    _ConfirmRefreshView,
    _ConfirmSaveAllView,
    _ConfirmTaxonomyCreationView,
    _DateSelect,
    _document_embed,
    _FailedUploadController,
    _FailedUploadView,
    _FinishReviewButton,
    _is_delivery_request,
    _is_follow_up,
    _MetadataOverflowView,
    _MetadataPageSelect,
    _MetadataSelect,
    _PendingUploadView,
    _RefreshReviewButton,
    _result_view,
    _ReviewActionsView,
    _ReviewThreadController,
    _ReviewThreadControlsView,
    _TitleEditView,
    _UploadBatchController,
    _UploadBatchControlsView,
)
from paperless_assistant.errors import (
    InvalidAttachmentError,
    PaperlessPermissionError,
    PaperlessUnavailableError,
    RateLimitedError,
    StaleSuggestionError,
    UnlinkedUserError,
)
from paperless_assistant.models import (
    AISuggestions,
    DeliveryPlan,
    DiscordMessageTarget,
    Document,
    DocumentId,
    DocumentUpdate,
    Download,
    IngestionJob,
    JobState,
    MetadataGuidance,
    ReferenceContext,
    SuggestedDate,
    SuggestionApplyResult,
    SuggestionReview,
    SuggestionSelection,
    Taxonomy,
    TaxonomyCapabilities,
    TaxonomyItem,
    TaxonomyKind,
    UploadBatch,
    UploadBatchSnapshot,
    UploadItem,
    UploadItemState,
)
from paperless_assistant.services import IngestionOutcome, IngestionService, QueryResponse


class FakeChannel:
    def __init__(self, identifier: int) -> None:
        self.id = identifier
        self.sent: list[FakeMessage] = []
        self.partial: list[FakeMessage] = []
        self.threads: list[FakeThread] = []
        self.archived: list[FakeThread] = []
        self.history_factory: Callable[[int], Any] | None = None

    async def send(self, content: str, **kwargs: Any) -> FakeMessage:
        message = FakeMessage(
            channel=self,
            content=content,
            identifier=1000 + len(self.sent),
        )
        message.send_kwargs = kwargs
        self.sent.append(message)
        return message

    def get_partial_message(self, identifier: int) -> FakeMessage:
        message = FakeMessage(channel=self, identifier=identifier)
        self.partial.append(message)
        return message

    def history(self, limit: int = 100) -> Any:
        if self.history_factory is not None:
            return self.history_factory(limit)

        async def iterator() -> Any:
            if False:
                yield None

        return iterator()

    def archived_threads(self, *, limit: int = 100) -> Any:
        del limit

        async def iterator() -> Any:
            for thread in self.archived:
                yield thread

        return iterator()


class FakeMessage:
    def __init__(  # noqa: PLR0913
        self,
        *,
        channel: FakeChannel,
        content: str = "",
        identifier: int = 500,
        attachments: Sequence[Any] = (),
        user_id: int = 201,
        guild_id: int | None = 100,
    ) -> None:
        self.id = identifier
        self.channel = channel
        self.content = content
        self.attachments = list(attachments)
        self.author = SimpleNamespace(id=user_id, bot=False)
        self.guild = (
            SimpleNamespace(id=guild_id, filesize_limit=10 * 1024 * 1024)
            if guild_id is not None
            else None
        )
        self.webhook_id = None
        self.reference: Any = None
        self.replies: list[FakeMessage] = []
        self.edits: list[dict[str, Any]] = []
        self.deleted = False
        self.pinned = False
        self.send_kwargs: dict[str, Any] = {}

    async def reply(self, content: str, **kwargs: Any) -> FakeMessage:
        sent = FakeMessage(
            channel=self.channel,
            content=content,
            identifier=700 + len(self.replies),
        )
        sent.send_kwargs = kwargs
        self.replies.append(sent)
        return sent

    async def create_thread(self, name: str, auto_archive_duration: int = 1440) -> FakeThread:
        thread = FakeThread(parent_id=self.channel.id, thread_id=3000 + self.id, name=name)
        self.thread = thread
        self.channel.threads.append(thread)
        return thread

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]

    async def delete(self) -> None:
        self.deleted = True


class FakeThread(discord.Thread):
    def __init__(self, parent_id: int, thread_id: int = 3500, name: str = "Thread") -> None:
        self.id = thread_id
        self.parent_id = parent_id
        parent = FakeChannel(parent_id)
        self.name = name
        self.sent: list[FakeMessage] = []
        self.guild = cast(
            discord.Guild,
            SimpleNamespace(
                id=100,
                filesize_limit=10 * 1024 * 1024,
                get_channel=lambda identifier: parent if identifier == parent_id else None,
            ),
        )
        self.archived = False
        self.locked = False
        self.deleted = False
        self.owner_id = 999
        self.added_users: list[Any] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        message = FakeMessage(
            channel=cast(Any, self),
            content=content or "",
            identifier=1000 + len(self.sent),
        )
        message.send_kwargs = kwargs
        self.sent.append(message)
        return message

    async def edit(  # type: ignore[override]
        self, *, archived: bool, locked: bool, reason: str
    ) -> None:
        assert reason
        self.archived = archived
        self.locked = locked

    async def add_user(self, user: Any) -> None:
        self.added_users.append(user)

    def get_partial_message(self, identifier: int) -> FakeMessage:  # type: ignore[override]
        existing = next((message for message in self.sent if message.id == identifier), None)
        if existing is not None:
            return existing
        message = FakeMessage(channel=cast(Any, self), identifier=identifier)
        self.sent.append(message)
        return message

    def history(self, limit: int = 100) -> Any:  # type: ignore[override]
        del limit

        async def iterator() -> Any:
            for message in reversed(self.sent):
                yield message

        return iterator()

    async def delete(self, *, reason: str | None = None) -> None:
        assert reason
        self.deleted = True


class FakeAttachment:
    def __init__(
        self,
        identifier: int,
        filename: str,
        content: bytes,
        *,
        declared_size: int | None = None,
        fail: bool = False,
    ) -> None:
        self.id = identifier
        self.filename = filename
        self.content = content
        self.size = declared_size if declared_size is not None else len(content)
        self.fail = fail

    async def save(self, destination: Path, *, use_cached: bool) -> None:
        assert not use_cached
        if self.fail:
            raise discord.HTTPException(
                cast(Any, SimpleNamespace(status=500, reason="synthetic")),
                "synthetic",
            )
        destination.write_bytes(self.content)  # noqa: ASYNC240


class FakeQuery:
    def __init__(self) -> None:
        self.current_context: ReferenceContext | None = None
        self.response = QueryResponse(
            "Native answer",
            (Document(DocumentId(7), "Synthetic", date(2024, 1, 2)),),
            False,
            uuid4(),
        )
        self.similar_response = QueryResponse(
            "Documents similar to Paperless document #7:",
            (Document(DocumentId(8), "Similar", date(2024, 2, 3)),),
            False,
            uuid4(),
        )
        self.error: Exception | None = None
        self.asked: list[tuple[int, str, int | None]] = []
        self.similar_requests: list[tuple[int, int, int | None]] = []
        self.saved: tuple[int, tuple[DocumentId, ...], tuple[int, ...]] | None = None

    async def context(self, principal_id: int) -> ReferenceContext | None:
        return self.current_context

    async def ask(
        self,
        principal_id: int,
        question: str,
        *,
        document_id: int | None = None,
        context_id: int | None = None,
    ) -> QueryResponse:
        self.asked.append((principal_id, question, document_id))
        if self.error:
            raise self.error
        return self.response

    async def save_rendered_context(
        self,
        principal_id: int,
        document_ids: tuple[DocumentId, ...],
        result_message_ids: tuple[int, ...],
    ) -> None:
        self.saved = (principal_id, document_ids, result_message_ids)

    async def find_similar(
        self,
        principal_id: int,
        document_id: int,
        *,
        context_id: int | None = None,
    ) -> QueryResponse:
        self.similar_requests.append((principal_id, document_id, context_id))
        if self.error:
            raise self.error
        return self.similar_response


class FakeTaxonomy:
    def __init__(self, ready: bool = True) -> None:
        self.ingestion_ready = ready
        self.refresh_result = ready

    @property
    def snapshot(self) -> Any:
        return Taxonomy(
            tags=(TaxonomyItem(1, "Discord"), TaxonomyItem(2, "Tag 2"), TaxonomyItem(3, "Tag 3")),
            correspondents=(TaxonomyItem(1, "Corr 1"),),
            document_types=(TaxonomyItem(1, "Type 1"),),
        )

    async def refresh(self) -> bool:
        self.ingestion_ready = self.refresh_result
        return self.refresh_result


class FakeIngestion:
    def __init__(self) -> None:
        self.stage_error: Exception | None = None
        self.stage_duplicate = False
        self.submit_state = JobState.SUBMITTED
        self.submit_duplicate_attachments: set[int] = set()
        self.poll_outcome: IngestionOutcome | None = None
        self.recovered = False
        self.office_dependent = False
        self.last_stage_kwargs: dict[str, Any] = {}
        self.warning_marker: tuple[int, datetime] | None = None
        self.suggestions_error: Exception | None = None
        self.upload_batch_record: UploadBatch | None = None
        self.upload_items: dict[int, UploadItem] = {}
        self.confirmed_cleanup: list[DiscordMessageTarget] = []
        self.active_outcomes: tuple[IngestionOutcome, ...] = ()
        self.active_items_for_restore: tuple[UploadItem, ...] = ()
        self.finalization_notifications: list[UUID] = []
        self.applied_updates: DocumentUpdate | None = None

    async def create_upload_batch(
        self,
        batch: UploadBatch,
        items: tuple[UploadItem, ...],
    ) -> None:
        self.upload_batch_record = batch
        self.upload_items = {item.attachment_id: item for item in items}

    async def update_upload_item(
        self,
        source_message_id: int,
        attachment_id: int,
        state: UploadItemState,
        **kwargs: Any,
    ) -> UploadBatchSnapshot:
        assert self.upload_batch_record is not None
        assert source_message_id == self.upload_batch_record.source_message_id
        current = self.upload_items[attachment_id]
        updates = {
            key: value
            for key, value in kwargs.items()
            if value is not None and hasattr(current, key)
        }
        self.upload_items[attachment_id] = replace(current, state=state, **updates)
        return UploadBatchSnapshot(
            self.upload_batch_record,
            tuple(sorted(self.upload_items.values(), key=lambda item: item.ordinal)),
        )

    async def resolve_upload_item(
        self,
        source_message_id: int,
        attachment_id: int,
        state: UploadItemState,
    ) -> tuple[DiscordMessageTarget, ...]:
        snapshot = await self.update_upload_item(
            source_message_id,
            attachment_id,
            state,
        )
        return snapshot.cleanup_targets if snapshot.cleanup_ready else ()

    async def confirm_upload_cleanup(
        self,
        targets: tuple[DiscordMessageTarget, ...],
    ) -> None:
        self.confirmed_cleanup.extend(targets)

    async def terminal_upload_cleanup_targets(
        self,
    ) -> tuple[DiscordMessageTarget, ...]:
        if self.upload_batch_record is None:
            return ()
        snapshot = UploadBatchSnapshot(
            self.upload_batch_record,
            tuple(sorted(self.upload_items.values(), key=lambda item: item.ordinal)),
        )
        if any(
            item.state
            in {
                UploadItemState.PENDING,
                UploadItemState.PROCESSING,
                UploadItemState.RECONCILIATION_REQUIRED,
            }
            for item in snapshot.items
        ):
            return ()
        return snapshot.cleanup_targets

    async def upload_item_for_job(self, job_id: Any) -> UploadItem | None:
        return next(
            (item for item in self.upload_items.values() if item.job_id == job_id),
            None,
        )

    async def upload_batch(self, source_message_id: int) -> UploadBatchSnapshot | None:
        if (
            self.upload_batch_record is None
            or self.upload_batch_record.source_message_id != source_message_id
        ):
            return None
        return UploadBatchSnapshot(
            self.upload_batch_record,
            tuple(sorted(self.upload_items.values(), key=lambda item: item.ordinal)),
        )

    async def active_upload_outcomes(self) -> tuple[IngestionOutcome, ...]:
        return self.active_outcomes

    async def active_upload_items(self) -> tuple[UploadItem, ...]:
        return self.active_items_for_restore

    async def tracked_upload_items(self) -> tuple[UploadItem, ...]:
        return tuple(sorted(self.upload_items.values(), key=lambda item: item.ordinal))

    async def resolved_upload_items_pending_cleanup(self) -> tuple[UploadItem, ...]:
        return tuple(
            item
            for item in await self.tracked_upload_items()
            if item.state in {UploadItemState.CLOSED, UploadItemState.DISMISSED}
            and (
                (item.parent_message_id is not None and not item.parent_cleaned)
                or (item.thread_id is not None and not item.thread_cleaned)
            )
        )

    async def confirm_upload_item_cleanup(
        self,
        source_message_id: int,
        attachment_id: int,
        *,
        parent_cleaned: bool,
        thread_cleaned: bool,
    ) -> None:
        current = self.upload_items[attachment_id]
        assert current.source_message_id == source_message_id
        self.upload_items[attachment_id] = replace(
            current,
            parent_cleaned=current.parent_cleaned or parent_cleaned,
            thread_cleaned=current.thread_cleaned or thread_cleaned,
        )

    async def stage(self, **kwargs: Any) -> IngestionJob | None:
        self.last_stage_kwargs = kwargs
        if self.stage_error:
            raise self.stage_error
        if self.stage_duplicate:
            return None
        job = IngestionJob(
            id=uuid4(),
            discord_message_id=kwargs["discord_message_id"],
            discord_attachment_id=kwargs["discord_attachment_id"],
            discord_status_message_id=kwargs["discord_status_message_id"],
            discord_message_channel_id=kwargs["discord_message_channel_id"],
            discord_status_channel_id=kwargs["discord_status_channel_id"],
            principal_id=kwargs["principal_id"],
            staged_path=kwargs["staged_path"],
            original_filename=kwargs["original_filename"],
            media_type="application/pdf",
            office_dependent=self.office_dependent,
            caption=kwargs["caption"],
            guidance=MetadataGuidance((1,), None, None),
        )
        if self.upload_batch_record is not None:
            await self.update_upload_item(
                kwargs["discord_message_id"],
                kwargs["discord_attachment_id"],
                UploadItemState.PENDING,
                job_id=job.id,
            )
        return job

    async def submit(self, job: IngestionJob) -> IngestionOutcome:
        duplicate_confirmed = job.discord_attachment_id in self.submit_duplicate_attachments
        submitted = IngestionJob(
            id=job.id,
            discord_message_id=job.discord_message_id,
            discord_attachment_id=job.discord_attachment_id,
            discord_status_message_id=job.discord_status_message_id,
            discord_message_channel_id=job.discord_message_channel_id,
            discord_status_channel_id=job.discord_status_channel_id,
            principal_id=job.principal_id,
            staged_path=job.staged_path,
            original_filename=job.original_filename,
            media_type=job.media_type,
            office_dependent=job.office_dependent,
            caption=job.caption,
            guidance=job.guidance,
            state=JobState.FAILED if duplicate_confirmed else self.submit_state,
            paperless_task_id=uuid4(),
            duplicate_confirmed=duplicate_confirmed,
        )
        return IngestionOutcome(submitted)

    async def poll_until_notifiable(self, job: IngestionJob) -> IngestionOutcome:
        if self.poll_outcome is not None:
            return self.poll_outcome
        succeeded = IngestionJob(
            id=job.id,
            discord_message_id=job.discord_message_id,
            discord_attachment_id=job.discord_attachment_id,
            discord_status_message_id=job.discord_status_message_id,
            discord_message_channel_id=job.discord_message_channel_id,
            discord_status_channel_id=job.discord_status_channel_id,
            principal_id=job.principal_id,
            staged_path=job.staged_path,
            original_filename=job.original_filename,
            media_type=job.media_type,
            office_dependent=job.office_dependent,
            caption=job.caption,
            guidance=job.guidance,
            state=JobState.SUCCEEDED,
            paperless_task_id=job.paperless_task_id,
            paperless_document_id=DocumentId(44),
        )
        return IngestionOutcome(
            succeeded,
            Document(DocumentId(44), "Uploaded", date(2024, 1, 1)),
        )

    async def recover(self, notify: Any = None) -> None:
        self.recovered = True

    async def message_succeeded(self, discord_message_id: int) -> bool:
        return discord_message_id > 0

    async def warning_state(self) -> tuple[int, datetime] | None:
        return self.warning_marker

    async def record_warning(self, message_id: int, emitted_at: datetime) -> None:
        self.warning_marker = (message_id, emitted_at)

    async def clear_warning(self) -> None:
        self.warning_marker = None

    async def get_suggestion_review(self, job: IngestionJob) -> SuggestionReview | None:
        if self.suggestions_error:
            raise self.suggestions_error
        suggestions = getattr(
            self,
            "suggestions",
            AISuggestions(
                title="Fake Title",
                correspondent_ids=(1,),
                document_type_ids=(1,),
                tag_ids=(2,),
            ),
        )
        if suggestions is None:
            return None
        return SuggestionReview(
            Document(
                DocumentId(job.paperless_document_id or 44),
                "Reloaded",
                date(2024, 1, 1),
                modified=datetime(2026, 7, 29, tzinfo=UTC),
            ),
            suggestions,
            FakeTaxonomy().snapshot,
            TaxonomyCapabilities(True, True, True, True),
        )

    @staticmethod
    def initial_suggestion_selection(review: SuggestionReview) -> SuggestionSelection:
        return IngestionService.initial_suggestion_selection(review)

    async def resolve_or_create_taxonomy(
        self,
        job: IngestionJob,
        kind: TaxonomyKind,
        name: str,
        *,
        confirm_create: bool,
        storage_path: str | None = None,
    ) -> TaxonomyItem:
        assert confirm_create
        del job, storage_path
        return TaxonomyItem(100 + list(TaxonomyKind).index(kind), name)

    async def apply_suggestions(
        self,
        job: IngestionJob,
        updates: DocumentUpdate,
        *,
        expected_modified: datetime | None,
    ) -> SuggestionApplyResult:
        self.applied_updates = updates
        return SuggestionApplyResult(
            Document(
                DocumentId(7),
                updates.title or "Current",
                updates.created,
                modified=expected_modified,
                tag_ids=updates.tag_ids or (2,),
                correspondent_id=updates.correspondent_id,
                document_type_id=updates.document_type_id,
                storage_path_id=updates.storage_path_id,
            ),
            True,
        )

    async def mark_review_finalization_notified(self, job: IngestionJob) -> None:
        self.finalization_notifications.append(job.id)

    async def check_inbox_tag_removals(self) -> tuple[DiscordMessageTarget, ...]:
        return ()


class FakeDelivery:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.mode = "original"
        self.cleaned: list[DeliveryPlan] = []

    async def prepare(
        self, principal_id: int, document_id: int, attachment_limit: int
    ) -> DeliveryPlan:
        assert principal_id == 201
        assert attachment_limit > 0
        if self.mode == "error":
            raise PaperlessUnavailableError("synthetic")
        if self.mode == "link":
            return DeliveryPlan(
                DocumentId(document_id),
                None,
                f"https://paperless.example.test/download/{document_id}",
            )
        path = self.directory / str(uuid4())
        path.write_bytes(b"synthetic")
        return DeliveryPlan(
            DocumentId(document_id),
            Download(path, "Formatted.pdf", "application/pdf", 9),
            f"https://paperless.example.test/download/{document_id}",
            used_archived=self.mode == "archived",
        )

    def cleanup(self, plan: DeliveryPlan) -> None:
        self.cleaned.append(plan)
        if plan.attachment:
            plan.attachment.path.unlink(missing_ok=True)


def _assistant(
    settings: Settings,
    query: FakeQuery,
    ingestion: FakeIngestion,
    delivery: FakeDelivery,
    taxonomy: FakeTaxonomy,
) -> DiscordAssistant:
    return DiscordAssistant(
        settings,
        cast(Any, query),
        cast(Any, ingestion),
        cast(Any, delivery),
        cast(Any, taxonomy),
        ready_callback=lambda _: None,
    )


def _context(*documents: int, message_ids: tuple[int, ...] = ()) -> ReferenceContext:
    return ReferenceContext(
        201,
        tuple(DocumentId(item) for item in documents),
        datetime.now(tz=UTC) + timedelta(minutes=5),
        message_ids,
    )


def _latest_upload_summary(channel: FakeChannel) -> FakeMessage:
    return next(
        message
        for message in reversed(channel.sent)
        if isinstance(message.send_kwargs.get("view"), _UploadBatchControlsView)
    )


def _job(
    tmp_path: Path,
    state: JobState,
    *,
    office_dependent: bool = False,
) -> IngestionJob:
    return IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=tmp_path / "staged",
        original_filename="synthetic.pdf",
        media_type="application/pdf",
        office_dependent=office_dependent,
        caption="",
        guidance=MetadataGuidance(),
        state=state,
        paperless_task_id=uuid4(),
    )


def test_language_helpers() -> None:
    document = Document(DocumentId(7), "Synthetic", date(2024, 1, 2))

    assert _is_delivery_request("Please send the file")
    assert not _is_delivery_request("Where is it?")
    assert _is_follow_up("What about the date?")
    assert not _is_follow_up("Find a vaccine record")

    embed = _document_embed(document, "https://example.test")
    assert embed.title == "📄 Synthetic"
    assert embed.fields[0].value == "Jan 02, 2024"
    assert embed.fields[1].value == "7"

    undated = _document_embed(Document(DocumentId(8), "No Date", None), "https://example.test")
    assert undated.fields[0].value == "Unavailable"


@pytest.mark.asyncio
async def test_view_and_exact_message_routing(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    assistant = _assistant(
        settings,
        query,
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    view = _result_view(201, 7, "https://paperless.example.test/doc")
    assert len(view.children) == 3
    assert [getattr(child, "label", None) for child in view.children] == [
        "Open in Paperless",
        "Send File",
        "Similar",
    ]
    assert getattr(view.children[-1], "custom_id", None) == "paperless:similar:201:7"

    questions = AsyncMock()
    uploads = AsyncMock()
    cast(Any, assistant)._questions_message = questions
    cast(Any, assistant)._uploads_message = uploads
    question_message = FakeMessage(channel=FakeChannel(settings.discord_questions_channel_id))
    await assistant.on_message(cast(discord.Message, question_message))
    questions.assert_awaited_once()

    upload_message = FakeMessage(channel=FakeChannel(settings.discord_uploads_channel_id))
    await assistant.on_message(cast(discord.Message, upload_message))
    uploads.assert_awaited_once()

    other_channel = FakeMessage(channel=FakeChannel(999))
    await assistant.on_message(cast(discord.Message, other_channel))
    assert questions.await_count == 1
    assert uploads.await_count == 1

    unauthorized = FakeMessage(
        channel=FakeChannel(settings.discord_questions_channel_id),
        user_id=999,
    )
    await assistant.on_message(cast(discord.Message, unauthorized))
    assert questions.await_count == 1
    unauthorized.guild = None
    assert not assistant._authorized_message(cast(discord.Message, unauthorized))

    failed = FakeMessage(channel=FakeChannel(settings.discord_questions_channel_id))
    await assistant.on_error("on_message", cast(discord.Message, failed))
    assert "Reference" in failed.replies[0].content
    ignored_failure = FakeMessage(
        channel=FakeChannel(settings.discord_questions_channel_id),
        user_id=999,
    )
    await assistant.on_error("on_message", cast(discord.Message, ignored_failure))
    assert not ignored_failure.replies
    await assistant.close()


@pytest.mark.asyncio
async def test_questions_guidance_answer_followup_and_errors(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    delivery = FakeDelivery(tmp_path)
    assistant = _assistant(settings, query, FakeIngestion(), delivery, FakeTaxonomy())
    channel = FakeChannel(settings.discord_questions_channel_id)

    attached = FakeMessage(channel=channel, attachments=[object()])
    await assistant._questions_message(cast(discord.Message, attached))
    assert "upload documents" in attached.replies[0].content

    empty = FakeMessage(channel=channel, content=" ")
    await assistant._questions_message(cast(discord.Message, empty))
    assert not empty.replies

    question = FakeMessage(channel=channel, content="Find my record")
    await assistant._questions_message(cast(discord.Message, question))
    assert question.thread.sent[0].content == "Native answer"
    assert query.asked[-1] == (201, "Find my record", None)
    assert query.saved is not None
    assert question.thread.sent[1].send_kwargs["embed"].title.startswith("📄 ")

    query.current_context = _context(7, message_ids=(1234,))
    followup = FakeMessage(channel=channel, content="What about its date?")
    followup.reference = SimpleNamespace(message_id=1234)
    await assistant._questions_message(cast(discord.Message, followup))
    assert query.asked[-1][2] == 7

    query.current_context = _context(7, 8)
    ambiguous = FakeMessage(channel=channel, content="What about the date?")
    await assistant._questions_message(cast(discord.Message, ambiguous))
    assert "Which result" in ambiguous.thread.sent[0].content

    query.current_context = _context(7)
    single = FakeMessage(channel=channel, content="Tell me more")
    await assistant._questions_message(cast(discord.Message, single))
    assert query.asked[-1][2] == 7

    delivery_without_ordinal = FakeMessage(channel=channel, content="send it please")
    await assistant._questions_message(cast(discord.Message, delivery_without_ordinal))
    assert query.asked[-1] == (201, "send it please", None)

    query.current_context = _context(message_ids=(1234,))
    invalid_reply = FakeMessage(channel=channel, content="Tell me more")
    invalid_reply.reference = SimpleNamespace(message_id=1234)
    await assistant._questions_message(cast(discord.Message, invalid_reply))
    assert not hasattr(invalid_reply, "thread") or not invalid_reply.thread.sent

    query.current_context = None
    query.error = RateLimitedError("synthetic")
    limited = FakeMessage(channel=channel, content="question")
    await assistant._questions_message(cast(discord.Message, limited))
    assert "quickly" in limited.thread.sent[0].content

    query.error = PaperlessUnavailableError("synthetic")
    unavailable = FakeMessage(channel=channel, content="question")
    await assistant._questions_message(cast(discord.Message, unavailable))
    assert "unavailable" in unavailable.thread.sent[0].content
    await assistant.close()


@pytest.mark.asyncio
async def test_conversational_and_direct_delivery(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    query.current_context = _context(7, 8)
    delivery = FakeDelivery(tmp_path)
    assistant = _assistant(settings, query, FakeIngestion(), delivery, FakeTaxonomy())
    channel = FakeChannel(settings.discord_questions_channel_id)

    send_second = FakeMessage(channel=channel, content="send me the second one")
    await assistant._questions_message(cast(discord.Message, send_second))
    assert send_second.thread.sent[-1].send_kwargs["file"].filename == "Formatted.pdf"
    assert delivery.cleaned

    delivery.mode = "link"
    await assistant._deliver_to_message(cast(discord.Message, send_second), (7,))
    assert "too large" in send_second.replies[-1].content

    delivery.mode = "archived"
    await assistant._deliver_to_message(cast(discord.Message, send_second), (7,))
    assert "Archived" in send_second.replies[-1].content

    delivery.mode = "error"
    await assistant._deliver_to_message(cast(discord.Message, send_second), (7,))
    assert "unavailable" in send_second.replies[-1].content

    unavailable_number = FakeMessage(channel=channel, content="send me the third one")
    await assistant._questions_message(cast(discord.Message, unavailable_number))
    assert "not available" in unavailable_number.thread.sent[-1].content
    await assistant.close()


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.sent.append(content)

    async def defer(self, **kwargs: Any) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.sent.append({"content": content, **kwargs})


@pytest.mark.asyncio
async def test_persistent_delivery_interaction(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    delivery = FakeDelivery(tmp_path)
    assistant = _assistant(settings, query, FakeIngestion(), delivery, FakeTaxonomy())

    expired = SimpleNamespace(
        data={"custom_id": "paperless:send:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=settings.discord_questions_channel_id,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
    )
    await assistant.on_interaction(cast(discord.Interaction, expired))
    assert "expired" in expired.response.sent[0]

    query.current_context = _context(7)
    valid = SimpleNamespace(
        data={"custom_id": "paperless:send:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=settings.discord_questions_channel_id,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
        guild=SimpleNamespace(id=100, filesize_limit=10 * 1024 * 1024),
    )
    await assistant.on_interaction(cast(discord.Interaction, valid))
    assert valid.followup.sent[0]["file"].filename == "Formatted.pdf"

    delivery.mode = "link"
    link_valid = SimpleNamespace(
        data={"custom_id": "paperless:send:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=settings.discord_questions_channel_id,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
        guild=SimpleNamespace(id=100, filesize_limit=10 * 1024 * 1024),
    )
    await assistant.on_interaction(cast(discord.Interaction, link_valid))
    assert "Too large" in link_valid.followup.sent[0]["content"]

    delivery.mode = "archived"
    archived_valid = SimpleNamespace(
        data={"custom_id": "paperless:send:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=settings.discord_questions_channel_id,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
        guild=SimpleNamespace(id=100, filesize_limit=10 * 1024 * 1024),
    )
    await assistant.on_interaction(cast(discord.Interaction, archived_valid))
    assert "Archived" in archived_valid.followup.sent[0]["content"]

    delivery.mode = "error"
    err_valid = SimpleNamespace(
        data={"custom_id": "paperless:send:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=settings.discord_questions_channel_id,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
        guild=SimpleNamespace(id=100, filesize_limit=10 * 1024 * 1024),
    )
    await assistant.on_interaction(cast(discord.Interaction, err_valid))
    assert "unavailable" in err_valid.followup.sent[0]["content"]

    ignored = SimpleNamespace(
        data={"custom_id": "other:action"},
        response=FakeInteractionResponse(),
    )
    await assistant.on_interaction(cast(discord.Interaction, ignored))
    assert not ignored.response.sent
    await assistant.close()


@pytest.mark.asyncio
async def test_similar_interaction_renders_owner_scoped_results(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    query.current_context = _context(7)
    assistant = _assistant(
        settings,
        query,
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    thread = FakeThread(settings.discord_questions_channel_id, thread_id=5001)
    interaction = SimpleNamespace(
        data={"custom_id": "paperless:similar:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=thread.id,
        channel=thread,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
    )

    await assistant.on_interaction(cast(discord.Interaction, interaction))

    assert interaction.response.deferred
    assert query.similar_requests == [(201, 7, 5001)]
    assert thread.sent[0].content == "Documents similar to Paperless document #7:"
    result_view = thread.sent[1].send_kwargs["view"]
    assert getattr(result_view.children[-1], "custom_id", None) == "paperless:similar:201:8"
    assert query.saved is not None
    assert query.saved[0] == 5001
    assert interaction.followup.sent[0]["content"] == "Similar results were posted in this thread."
    await assistant.close()


@pytest.mark.asyncio
async def test_similar_interaction_empty_result(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    query.current_context = _context(7)
    query.similar_response = QueryResponse(
        "No similar documents were found for Paperless document #7.",
        (),
        False,
        uuid4(),
    )
    assistant = _assistant(
        settings,
        query,
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    thread = FakeThread(settings.discord_questions_channel_id, thread_id=5001)
    interaction = SimpleNamespace(
        data={"custom_id": "paperless:similar:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=thread.id,
        channel=thread,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
    )

    await assistant.on_interaction(cast(discord.Interaction, interaction))

    assert len(thread.sent) == 1
    assert thread.sent[0].content == ("No similar documents were found for Paperless document #7.")
    await assistant.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        ("paperless:similar:201:7", 202, True, "expired or is unavailable"),
        ("paperless:similar:201:7", 201, False, "expired or is unavailable"),
        ("paperless:similar:invalid", 201, True, "invalid or has expired"),
    ],
)
async def test_similar_interactions_fail_gracefully(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    case: tuple[str, int, bool, str],
) -> None:
    custom_id, user_id, has_context, expected = case
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    query.current_context = _context(7) if has_context else None
    assistant = _assistant(
        settings,
        query,
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    thread = FakeThread(settings.discord_questions_channel_id, thread_id=5001)
    interaction = SimpleNamespace(
        data={"custom_id": custom_id},
        guild_id=settings.discord_guild_id,
        channel_id=thread.id,
        channel=thread,
        user=SimpleNamespace(id=user_id),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
    )

    await assistant.on_interaction(cast(discord.Interaction, interaction))

    assert expected in interaction.response.sent[0]
    assert query.similar_requests == []
    await assistant.close()


@pytest.mark.asyncio
async def test_stale_dismiss_interaction_fails_gracefully(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    assistant = _assistant(
        settings,
        FakeQuery(),
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    interaction = SimpleNamespace(
        data={"custom_id": "paperless:dismiss:legacy"},
        response=FakeInteractionResponse(),
    )

    await assistant.on_interaction(cast(discord.Interaction, interaction))

    assert "no longer available" in interaction.response.sent[0]
    await assistant.close()


@pytest.mark.asyncio
async def test_similar_interaction_requires_thread(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    query.current_context = _context(7)
    assistant = _assistant(
        settings,
        query,
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_questions_channel_id)
    outside_thread = SimpleNamespace(
        data={"custom_id": "paperless:similar:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=channel.id,
        channel=channel,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
    )

    await assistant.on_interaction(cast(discord.Interaction, outside_thread))

    assert "expired or is unavailable" in outside_thread.response.sent[0]
    assert query.similar_requests == []
    await assistant.close()


@pytest.mark.asyncio
async def test_non_string_component_is_ignored(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    assistant = _assistant(
        settings,
        FakeQuery(),
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    interaction = SimpleNamespace(data=None, response=FakeInteractionResponse())

    await assistant.on_interaction(cast(discord.Interaction, interaction))

    assert interaction.response.sent == []
    await assistant.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RateLimitedError("synthetic"), "searched several times"),
        (UnlinkedUserError("synthetic"), "not linked"),
        (PaperlessPermissionError("synthetic"), "did not allow access"),
        (PaperlessUnavailableError("synthetic"), "could not complete"),
    ],
)
async def test_similar_interaction_maps_safe_errors(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    error: Exception,
    expected: str,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    query.current_context = _context(7)
    query.error = error
    assistant = _assistant(
        settings,
        query,
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    thread = FakeThread(settings.discord_questions_channel_id, thread_id=5001)
    interaction = SimpleNamespace(
        data={"custom_id": "paperless:similar:201:7"},
        guild_id=settings.discord_guild_id,
        channel_id=thread.id,
        channel=thread,
        user=SimpleNamespace(id=201),
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
    )

    await assistant.on_interaction(cast(discord.Interaction, interaction))

    assert expected in interaction.followup.sent[0]["content"]
    assert thread.sent == []
    await assistant.close()


@pytest.mark.asyncio
async def test_upload_guidance_missing_tag_and_partial_validation(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path,
        discord_max_attachment_bytes=20,
        ingestion_max_staged_bytes=25,
    )
    settings.staging_dir.mkdir(parents=True)
    query = FakeQuery()
    ingestion = FakeIngestion()
    taxonomy = FakeTaxonomy()
    assistant = _assistant(settings, query, ingestion, FakeDelivery(tmp_path), taxonomy)
    channel = FakeChannel(settings.discord_uploads_channel_id)

    text_only = FakeMessage(channel=channel, content="caption")
    await assistant._uploads_message(cast(discord.Message, text_only))
    assert "Attach" in text_only.replies[0].content

    taxonomy.ingestion_ready = False
    blocked = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(1, "one.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, blocked))
    assert "paused" in blocked.replies[0].content

    taxonomy.ingestion_ready = True
    orphan_thread = FakeThread(channel.id)
    orphan_thread.guild = cast(
        discord.Guild,
        SimpleNamespace(
            id=100,
            filesize_limit=10 * 1024 * 1024,
            get_channel=lambda _: None,
        ),
    )
    orphan = FakeMessage(
        channel=cast(Any, orphan_thread),
        attachments=[FakeAttachment(1, "orphan.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, orphan))
    assert "require access" in orphan.replies[0].content

    bare_thread = SimpleNamespace(id=3001)
    bare_parent = SimpleNamespace(
        id=2001,
        create_thread=AsyncMock(return_value=bare_thread),
    )
    bare_channel = SimpleNamespace(send=AsyncMock(return_value=bare_parent))
    created_parent, created_thread = await assistant._create_upload_item_thread(
        bare_channel,
        ordinal=1,
        total_items=1,
        filename="bare.pdf",
        parent_content="metadata",
        uploader=SimpleNamespace(id=201),
    )
    assert cast(Any, created_parent) is bare_parent
    assert cast(Any, created_thread) is bare_thread

    missing_document = IngestionOutcome(_job(tmp_path, JobState.SUCCEEDED))
    assert not await assistant._create_success_upload_review(
        channel=channel,
        message=cast(discord.Message, blocked),
        outcome=missing_document,
        ordinal=1,
        total_items=1,
        batch_controller=_UploadBatchController(201, 900),
    )

    partial = FakeMessage(
        channel=channel,
        content="caption",
        attachments=[
            FakeAttachment(1, "large.pdf", b"x", declared_size=21),
            FakeAttachment(2, "one.pdf", b"%PDF-1.7"),
            FakeAttachment(3, "quota.pdf", b"%PDF-1.7", declared_size=20),
        ],
    )
    await assistant._uploads_message(cast(discord.Message, partial))
    status = _latest_upload_summary(channel)
    combined = "\n".join(edit["content"] for edit in status.edits)
    assert "too large" in combined.casefold()
    assert "staging quota" in combined.casefold()
    assert "uploaded as" in combined
    assert not partial.deleted
    assert len([message for message in channel.sent if hasattr(message, "thread")]) == 3
    target = DiscordMessageTarget(channel.id, 42)
    assistant.cleanup_messages = AsyncMock(return_value=())  # type: ignore[method-assign]
    await assistant._cleanup_upload_targets((target,))
    assistant.cleanup_messages = AsyncMock(return_value=(target,))  # type: ignore[method-assign]
    await assistant._cleanup_upload_targets((target,))
    assert ingestion.confirmed_cleanup[-1] == target
    await assistant.close()


@pytest.mark.asyncio
async def test_upload_success_duplicate_invalid_and_uncertain(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.staging_dir.mkdir(parents=True)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)

    success = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(1, "one.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, success))
    status = _latest_upload_summary(channel)
    assert not success.deleted
    assert ingestion.last_stage_kwargs["discord_status_message_id"] == status.id
    success_parent = next(
        message
        for message in reversed(channel.sent)
        if hasattr(message, "thread") and "Paperless:" in message.content
    )
    assert isinstance(
        success_parent.thread.sent[-1].send_kwargs["view"],
        _ReviewThreadControlsView,
    )

    ingestion.stage_duplicate = True
    duplicate = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(2, "two.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, duplicate))
    assert not duplicate.deleted
    assert "already received" in _latest_upload_summary(channel).edits[-1]["content"]

    ingestion.stage_duplicate = False
    ingestion.stage_error = InvalidAttachmentError("bad signature")
    invalid = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(3, "three.pdf", b"bad")],
    )
    await assistant._uploads_message(cast(discord.Message, invalid))
    assert "bad signature" in _latest_upload_summary(channel).edits[-1]["content"]

    ingestion.stage_error = None
    ingestion.submit_state = JobState.RECONCILIATION_REQUIRED
    uncertain = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(4, "four.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, uncertain))
    assert "uncertain" in _latest_upload_summary(channel).edits[-1]["content"]
    await assistant.close()


@pytest.mark.asyncio
async def test_immediate_duplicate_uses_helpful_multi_file_message(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.staging_dir.mkdir(parents=True)
    ingestion = FakeIngestion()
    ingestion.submit_duplicate_attachments.add(1)
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)
    message = FakeMessage(
        channel=channel,
        attachments=[
            FakeAttachment(1, "duplicate.pdf", b"%PDF-1.7"),
            FakeAttachment(2, "different.pdf", b"%PDF-1.7"),
        ],
    )

    await assistant._uploads_message(cast(discord.Message, message))

    expected = (
        "Paperless identified a duplicate. Check/empty Paperless trash or upload a genuinely "
        "different file."
    )
    summary = _latest_upload_summary(channel).edits[-1]["content"]
    failed_parent = next(
        item for item in channel.sent if hasattr(item, "thread") and expected in item.content
    )
    assert expected in summary
    assert "uploaded as" in summary
    assert expected in failed_parent.thread.sent[-1].content
    await assistant.close()


@pytest.mark.asyncio
async def test_task_duplicate_uses_same_helpful_message(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.staging_dir.mkdir(parents=True)
    ingestion = FakeIngestion()
    ingestion.poll_outcome = IngestionOutcome(
        replace(
            _job(tmp_path, JobState.FAILED),
            discord_attachment_id=3,
            duplicate_confirmed=True,
        )
    )
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)
    message = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(3, "duplicate.pdf", b"%PDF-1.7")],
    )

    await assistant._uploads_message(cast(discord.Message, message))

    expected = (
        "Paperless identified a duplicate. Check/empty Paperless trash or upload a genuinely "
        "different file."
    )
    summary = _latest_upload_summary(channel).edits[-1]["content"]
    assert expected in summary
    assert "private upstream title" not in summary
    await assistant.close()


@pytest.mark.asyncio
async def test_upload_reports_link_and_ai_review_failures(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.staging_dir.mkdir(parents=True)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)

    ingestion.stage_error = UnlinkedUserError("synthetic")
    unlinked_stage = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(1, "one.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, unlinked_stage))
    assert "Link your Paperless" in _latest_upload_summary(channel).edits[-1]["content"]

    ingestion.stage_error = None
    for index, error in enumerate(
        (
            PaperlessUnavailableError("synthetic"),
            StaleSuggestionError("synthetic"),
            UnlinkedUserError("synthetic"),
        ),
        start=2,
    ):
        ingestion.suggestions_error = error
        message = FakeMessage(
            channel=channel,
            attachments=[FakeAttachment(index, f"{index}.pdf", b"%PDF-1.7")],
        )
        await assistant._uploads_message(cast(discord.Message, message))
        combined = "\n".join(edit["content"] for edit in _latest_upload_summary(channel).edits)
        assert "AI review unavailable" in combined

    await assistant.close()


@pytest.mark.asyncio
async def test_upload_limits_download_failures_and_rejected_submission(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path,
        discord_max_attachments=1,
        discord_max_attachment_bytes=10,
        ingestion_max_staged_bytes=20,
    )
    settings.staging_dir.mkdir(parents=True)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)

    too_many = FakeMessage(
        channel=channel,
        attachments=[
            FakeAttachment(1, "one.pdf", b"%PDF"),
            FakeAttachment(2, "two.pdf", b"%PDF"),
        ],
    )
    await assistant._uploads_message(cast(discord.Message, too_many))
    assert "Only the first 1" in _latest_upload_summary(channel).edits[-1]["content"]

    actual_too_large = FakeMessage(
        channel=channel,
        attachments=[
            FakeAttachment(3, "large.pdf", b"01234567890", declared_size=1),
        ],
    )
    await assistant._uploads_message(cast(discord.Message, actual_too_large))
    assert "downloaded file exceeds" in _latest_upload_summary(channel).edits[-1]["content"]

    failed_download = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(4, "failed.pdf", b"x", fail=True)],
    )
    await assistant._uploads_message(cast(discord.Message, failed_download))
    assert "download failed" in _latest_upload_summary(channel).edits[-1]["content"]

    ingestion.submit_state = JobState.FAILED
    rejected = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(5, "rejected.pdf", b"%PDF")],
    )
    await assistant._uploads_message(cast(discord.Message, rejected))
    assert "rejected" in _latest_upload_summary(channel).edits[-1]["content"]
    await assistant.close()


@pytest.mark.asyncio
async def test_upload_post_download_quota_and_poll_recovery_states(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(
        data_dir=tmp_path,
        discord_max_attachment_bytes=20,
        ingestion_max_staged_bytes=20,
    )
    settings.staging_dir.mkdir(parents=True)
    existing = settings.staging_dir / "existing"
    existing.write_bytes(b"0123456789")
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)

    quota = FakeMessage(
        channel=channel,
        attachments=[
            FakeAttachment(1, "quota.pdf", b"01234567890", declared_size=1),
        ],
    )
    await assistant._uploads_message(cast(discord.Message, quota))
    assert "staging quota was exceeded" in _latest_upload_summary(channel).edits[-1]["content"]

    existing.unlink()
    cast(Any, ingestion).poll_until_notifiable = AsyncMock(side_effect=RuntimeError("synthetic"))
    unavailable = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(2, "unavailable.pdf", b"%PDF")],
    )
    await assistant._uploads_message(cast(discord.Message, unavailable))
    assert "Status is unavailable" in _latest_upload_summary(channel).edits[-1]["content"]

    cast(Any, ingestion).poll_until_notifiable = AsyncMock(
        return_value=IngestionOutcome(
            _job(tmp_path, JobState.SUBMITTED),
            notification_timed_out=True,
        )
    )
    timed_out = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(3, "pending.pdf", b"%PDF")],
    )
    await assistant._uploads_message(cast(discord.Message, timed_out))
    assert "still processing" in _latest_upload_summary(channel).edits[-1]["content"]
    await assistant.close()


@pytest.mark.asyncio
async def test_office_task_failure_includes_setup_guidance(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path, paperless_office_uploads_enabled=True)
    settings.staging_dir.mkdir(parents=True)
    ingestion = FakeIngestion()
    ingestion.office_dependent = True
    failed_job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=tmp_path / "staged",
        original_filename="synthetic.docx",
        media_type="application/zip",
        office_dependent=True,
        caption="",
        guidance=MetadataGuidance(),
        state=JobState.FAILED,
    )
    ingestion.poll_outcome = IngestionOutcome(failed_job)
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    message = FakeMessage(
        channel=FakeChannel(settings.discord_uploads_channel_id),
        attachments=[FakeAttachment(2, "synthetic.docx", b"PK\x03\x04")],
    )

    await assistant._uploads_message(cast(discord.Message, message))

    status_content = _latest_upload_summary(message.channel).edits[-1]["content"]
    assert "Tika/Gotenberg" in status_content
    assert "macro detected" not in status_content
    await assistant.close()


@pytest.mark.asyncio
async def test_warning_recovery_and_status_helpers(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    ingestion = FakeIngestion()
    taxonomy = FakeTaxonomy(False)
    assistant = _assistant(settings, query, ingestion, FakeDelivery(tmp_path), taxonomy)
    channel = FakeChannel(settings.discord_uploads_channel_id)
    cast(Any, assistant).get_channel = lambda _: channel
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)

    await assistant._warn_missing_tag()
    await assistant._warn_missing_tag()
    assert len(channel.sent) == 1
    assert ingestion.warning_marker is not None
    await assistant._clear_missing_tag_warning()
    assert ingestion.warning_marker is None
    assert channel.partial[-1].deleted

    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=tmp_path / "staged",
        original_filename="private.pdf",
        media_type="application/pdf",
        office_dependent=False,
        caption="",
        guidance=MetadataGuidance(),
        state=JobState.FAILED,
    )
    await assistant._notify_recovery(IngestionOutcome(job))
    assert channel.sent[-1].content.endswith("processing failed.")

    cast(Any, assistant).get_channel = lambda _: None
    await assistant._notify_recovery(IngestionOutcome(job))
    await assistant._notify_recovery(IngestionOutcome(job))
    assert len(cast(Any, assistant)._pending_recovery) == 1
    cast(Any, assistant).get_channel = lambda _: channel
    await assistant._flush_recovery_notifications()
    assert not cast(Any, assistant)._pending_recovery

    succeeded = IngestionJob(
        id=job.id,
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=job.staged_path,
        original_filename=job.original_filename,
        media_type=job.media_type,
        office_dependent=False,
        caption="",
        guidance=MetadataGuidance(),
        state=JobState.SUCCEEDED,
    )
    await assistant._notify_recovery(IngestionOutcome(succeeded))
    assert channel.partial[-1].deleted

    targets = tuple(
        DiscordMessageTarget(channel_id=channel.id, message_id=message_id)
        for message_id in (10, 11, 12)
    )
    confirmed = await assistant.cleanup_messages(targets[:2], targets[2:])
    assert confirmed == targets
    assert [message.id for message in channel.partial[-3:]] == [10, 11, 12]
    assert all(message.deleted for message in channel.partial[-3:])

    status = FakeMessage(channel=channel)
    await assistant._replace_status(cast(discord.Message, status), ["x" * 2100])
    assert status.edits
    assert channel.sent

    custom_status = FakeMessage(channel=channel)
    custom_view = discord.ui.View()
    await assistant._replace_status(
        cast(discord.Message, custom_status),
        ["y" * 2100],
        view=custom_view,
    )
    assert custom_status.edits[-1]["view"] is custom_view
    assert "view" not in channel.sent[-1].send_kwargs

    edit_error = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="synthetic")),
        "synthetic",
    )
    failed_status = FakeMessage(channel=channel)
    failed_status.edit = AsyncMock(side_effect=edit_error)
    await assistant._replace_status(cast(discord.Message, failed_status), ["fallback"])
    assert channel.sent[-1].content == "fallback"

    failed_query_status = FakeMessage(channel=channel)
    failed_query_status.edit = AsyncMock(side_effect=edit_error)
    await assistant._render_query(
        cast(discord.Message, failed_query_status),
        QueryResponse("answer fallback", (), False, uuid4()),
        201,
    )
    assert channel.sent[-1].content == "answer fallback"

    multi_status = FakeMessage(channel=channel)
    await assistant._render_query(
        cast(discord.Message, multi_status),
        QueryResponse("x" * 2100, (), False, uuid4()),
        201,
    )
    assert channel.sent[-1].content
    await assistant.close()


@pytest.mark.asyncio
async def test_duplicate_recovery_updates_file_and_batch_summary(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(assistant, "get_channel", lambda _: channel)
    job = replace(
        _job(tmp_path, JobState.FAILED),
        discord_message_id=70,
        discord_attachment_id=71,
        duplicate_confirmed=True,
    )
    await ingestion.create_upload_batch(
        UploadBatch(70, channel.id, 72, channel.id, 201, 2),
        (
            UploadItem(
                70,
                71,
                1,
                "duplicate.pdf",
                state=UploadItemState.PROCESSING,
                job_id=job.id,
            ),
            UploadItem(
                70,
                73,
                2,
                "different.pdf",
                state=UploadItemState.SUCCEEDED,
            ),
        ),
    )

    await assistant._notify_recovery(IngestionOutcome(job))

    expected = (
        "Paperless identified a duplicate. Check/empty Paperless trash or upload a genuinely "
        "different file."
    )
    failed_parent = next(item for item in channel.sent if expected in item.content)
    summary = next(item for item in channel.partial if item.id == 72)
    assert expected in failed_parent.thread.sent[-1].content
    assert expected in summary.edits[-1]["content"]
    assert "2. `different.pdf` — upload succeeded; review ready." in summary.edits[-1]["content"]
    await assistant.close()


@pytest.mark.asyncio
async def test_legacy_office_duplicate_recovery_uses_helpful_message(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    assistant = _assistant(
        settings,
        FakeQuery(),
        FakeIngestion(),
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(assistant, "get_channel", lambda _: channel)
    job = replace(
        _job(tmp_path, JobState.FAILED, office_dependent=True),
        duplicate_confirmed=True,
    )

    await assistant._notify_recovery(IngestionOutcome(job))

    expected = (
        "Paperless identified a duplicate. Check/empty Paperless trash or upload a genuinely "
        "different file."
    )
    assert expected in channel.sent[-1].content
    await assistant.close()


@pytest.mark.asyncio
async def test_per_file_recovery_rebuilds_saved_artifacts(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(assistant, "get_channel", lambda identifier: channel)
    job = replace(
        _job(tmp_path, JobState.SUCCEEDED),
        discord_message_id=70,
        discord_attachment_id=71,
        principal_id=201,
        original_filename="recovered.pdf",
        paperless_document_id=DocumentId(44),
    )
    await ingestion.create_upload_batch(
        UploadBatch(70, channel.id, 72, channel.id, 201, 1),
        (
            UploadItem(
                70,
                71,
                1,
                "recovered.pdf",
                state=UploadItemState.PROCESSING,
                job_id=job.id,
            ),
        ),
    )
    outcome = IngestionOutcome(
        job,
        Document(DocumentId(44), "Recovered", date(2026, 7, 28)),
    )
    await assistant._notify_recovery(outcome)
    recovered_item = ingestion.upload_items[71]
    assert recovered_item.state is UploadItemState.SUCCEEDED
    assert recovered_item.parent_message_id is not None
    parent = next(message for message in channel.sent if hasattr(message, "thread"))
    assert "Recovered review ready" in parent.content
    assert isinstance(
        parent.thread.sent[-1].send_kwargs["view"],
        _ReviewThreadControlsView,
    )
    review = await ingestion.get_suggestion_review(job)
    assert review is not None
    helper_thread = FakeThread(channel.id)
    helper_view = await assistant._send_suggestions_ui(helper_thread, job, review)
    assert isinstance(helper_view, AISuggestionsView)

    existing_thread = FakeThread(channel.id, thread_id=74)
    await ingestion.create_upload_batch(
        UploadBatch(70, channel.id, 72, channel.id, 201, 1),
        (
            UploadItem(
                70,
                71,
                1,
                "recovered.pdf",
                state=UploadItemState.PROCESSING,
                job_id=job.id,
                parent_message_id=73,
                parent_channel_id=channel.id,
                thread_id=74,
            ),
        ),
    )
    ingestion.suggestions_error = PaperlessUnavailableError("synthetic")
    monkeypatch.setattr(
        assistant,
        "get_channel",
        lambda identifier: existing_thread if identifier == 74 else channel,
    )
    await assistant._notify_recovery(outcome)
    recovered_parent = next(item for item in channel.partial if item.id == 73)
    assert "AI review unavailable" in recovered_parent.edits[-1]["content"]
    ingestion.suggestions_error = None

    failure_job = replace(
        job,
        id=uuid4(),
        discord_message_id=80,
        discord_attachment_id=81,
        state=JobState.FAILED,
        paperless_document_id=None,
    )
    stored_thread = FakeThread(channel.id, thread_id=84)
    await ingestion.create_upload_batch(
        UploadBatch(80, channel.id, 82, channel.id, 201, 1),
        (
            UploadItem(
                80,
                81,
                1,
                "failed.pdf",
                state=UploadItemState.PROCESSING,
                job_id=failure_job.id,
                parent_message_id=83,
                parent_channel_id=channel.id,
                thread_id=84,
            ),
        ),
    )
    monkeypatch.setattr(
        assistant,
        "get_channel",
        lambda identifier: stored_thread if identifier == 84 else channel,
    )
    await assistant._notify_recovery(IngestionOutcome(failure_job))
    assert ingestion.upload_items[81].state is UploadItemState.FAILED
    assert isinstance(stored_thread.sent[-1].send_kwargs["view"], _FailedUploadView)

    created_failure_job = replace(
        failure_job,
        id=uuid4(),
        discord_message_id=85,
        discord_attachment_id=86,
    )
    await ingestion.create_upload_batch(
        UploadBatch(85, channel.id, 87, channel.id, 201, 1),
        (
            UploadItem(
                85,
                86,
                1,
                "created-failure.pdf",
                state=UploadItemState.PROCESSING,
                job_id=created_failure_job.id,
            ),
        ),
    )
    monkeypatch.setattr(assistant, "get_channel", lambda _: channel)
    await assistant._notify_recovery(IngestionOutcome(created_failure_job))
    assert ingestion.upload_items[86].thread_id is not None

    uncertain_job = replace(
        failure_job,
        id=uuid4(),
        discord_message_id=90,
        discord_attachment_id=91,
        state=JobState.RECONCILIATION_REQUIRED,
    )
    uncertain_thread = FakeThread(channel.id, thread_id=94)
    await ingestion.create_upload_batch(
        UploadBatch(90, channel.id, 92, channel.id, 201, 1),
        (
            UploadItem(
                90,
                91,
                1,
                "uncertain.pdf",
                state=UploadItemState.PROCESSING,
                job_id=uncertain_job.id,
                parent_message_id=93,
                parent_channel_id=channel.id,
                thread_id=94,
            ),
        ),
    )
    monkeypatch.setattr(
        assistant,
        "get_channel",
        lambda identifier: uncertain_thread if identifier == 94 else channel,
    )
    await assistant._notify_recovery(IngestionOutcome(uncertain_job))
    assert ingestion.upload_items[91].state is UploadItemState.RECONCILIATION_REQUIRED
    assert isinstance(uncertain_thread.sent[-1].send_kwargs["view"], _PendingUploadView)

    waiting_job = replace(
        uncertain_job,
        id=uuid4(),
        discord_message_id=100,
        discord_attachment_id=101,
    )
    await ingestion.create_upload_batch(
        UploadBatch(100, channel.id, 102, channel.id, 201, 1),
        (
            UploadItem(
                100,
                101,
                1,
                "waiting.pdf",
                state=UploadItemState.PROCESSING,
                job_id=waiting_job.id,
                parent_channel_id=channel.id,
            ),
        ),
    )
    monkeypatch.setattr(assistant, "get_channel", lambda _: None)
    await assistant._notify_recovery(IngestionOutcome(waiting_job))
    assert assistant._pending_recovery[-1].job.id == waiting_job.id
    await assistant._notify_recovery(IngestionOutcome(waiting_job))
    assert sum(item.job.id == waiting_job.id for item in assistant._pending_recovery) == 1

    close_channel = FakeChannel(channel.id)
    close_thread = FakeThread(channel.id, thread_id=120)
    closed_item = UploadItem(
        110,
        111,
        1,
        "closed.pdf",
        state=UploadItemState.CLOSED,
        parent_message_id=119,
        parent_channel_id=channel.id,
        thread_id=120,
    )
    await ingestion.create_upload_batch(
        UploadBatch(110, channel.id, 118, channel.id, 201, 1),
        (closed_item,),
    )
    rich_controller = _ReviewThreadController(
        201,
        900,
        source_message_id=110,
        attachment_id=111,
    )
    rich_batch_controller = _UploadBatchController(201, 900)
    rich_batch_controller.add(rich_controller)
    assistant._upload_batch_controllers[110] = rich_batch_controller
    monkeypatch.setattr(
        assistant,
        "get_channel",
        lambda identifier: close_thread if identifier == 120 else close_channel,
    )
    await assistant.close_upload_items((closed_item,))
    assert rich_controller.closed
    assert close_thread.deleted
    assert close_channel.partial[-1].deleted
    assert ingestion.upload_items[111].thread_cleaned
    assert ingestion.upload_items[111].parent_cleaned
    await assistant._restore_batch_summary(
        None,
        _UploadBatchController(201, 900),
    )
    monkeypatch.setattr(assistant, "get_channel", lambda _: None)
    snapshot = await ingestion.upload_batch(100)
    await assistant._restore_batch_summary(
        snapshot,
        _UploadBatchController(201, 900),
    )
    pending_restore = UploadItem(140, 141, 1, "interrupted.pdf")
    await ingestion.create_upload_batch(
        UploadBatch(140, channel.id, 142, channel.id, 201, 1),
        (pending_restore,),
    )
    ingestion.active_outcomes = ()
    ingestion.active_items_for_restore = (pending_restore,)
    assistant._restored_upload_items.clear()
    monkeypatch.setattr(assistant, "get_channel", lambda _: channel)
    await assistant._restore_active_upload_reviews()
    assert ingestion.upload_items[141].state is UploadItemState.FAILED
    restored_parent = next(
        message
        for message in reversed(channel.sent)
        if hasattr(message, "thread") and "interrupted.pdf" in message.content
    )
    assert isinstance(
        restored_parent.thread.sent[-1].send_kwargs["view"],
        _FailedUploadView,
    )
    await assistant._restore_active_upload_reviews()
    ingestion.active_items_for_restore = (
        UploadItem(140, 143, 2, "success.pdf", state=UploadItemState.SUCCEEDED),
    )
    await assistant._restore_active_upload_reviews()
    await assistant._restore_non_success_upload_item(
        UploadItem(999, 1, 1, "missing.pdf", state=UploadItemState.FAILED)
    )
    processing_restore = UploadItem(
        160,
        161,
        1,
        "processing.pdf",
        state=UploadItemState.PROCESSING,
        job_id=uuid4(),
        parent_message_id=162,
        parent_channel_id=channel.id,
        thread_id=163,
    )
    await ingestion.create_upload_batch(
        UploadBatch(160, channel.id, 164, channel.id, 201, 1),
        (processing_restore,),
    )
    processing_thread = FakeThread(channel.id, thread_id=163)
    monkeypatch.setattr(
        assistant,
        "get_channel",
        lambda identifier: processing_thread if identifier == 163 else channel,
    )
    await assistant._restore_non_success_upload_item(processing_restore)
    assert isinstance(
        processing_thread.sent[-1].send_kwargs["view"],
        _PendingUploadView,
    )

    await ingestion.create_upload_batch(
        UploadBatch(150, channel.id, 152, channel.id, 201, 1),
        (UploadItem(150, 151, 1, "unavailable.pdf", state=UploadItemState.FAILED),),
    )
    monkeypatch.setattr(assistant, "get_channel", lambda _: None)
    await assistant._restore_non_success_upload_item(ingestion.upload_items[151])

    restore_loader = ingestion.active_upload_outcomes
    cast(Any, ingestion).active_upload_outcomes = None
    await assistant._restore_active_upload_reviews()
    cast(Any, ingestion).active_upload_outcomes = restore_loader
    item_restore_loader = ingestion.active_upload_items
    cast(Any, ingestion).active_upload_items = None
    await assistant._restore_active_upload_reviews()
    cast(Any, ingestion).active_upload_items = item_restore_loader
    ingestion.active_items_for_restore = ()
    ingestion.active_outcomes = (outcome,)
    assistant._restored_upload_jobs.clear()
    assistant._notify_recovery = AsyncMock()  # type: ignore[method-assign]
    await assistant._restore_active_upload_reviews()
    assistant._restored_upload_jobs.add(outcome.job.id)
    await assistant._restore_active_upload_reviews()
    assistant._notify_recovery.assert_awaited_once_with(outcome)
    assert ingestion.applied_updates is None
    assert ingestion.finalization_notifications == []
    await assistant.close()


@pytest.mark.asyncio
async def test_background_loops_lifecycle_and_ready_paths(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    taxonomy = FakeTaxonomy(True)
    ready: list[bool] = []
    assistant = DiscordAssistant(
        settings,
        cast(Any, FakeQuery()),
        cast(Any, ingestion),
        cast(Any, FakeDelivery(tmp_path)),
        cast(Any, taxonomy),
        ready_callback=ready.append,
    )

    async def complete() -> None:
        return

    assistant._start_background(complete())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not cast(Any, assistant)._background_tasks

    blocker = asyncio.Event()

    async def wait_for_blocker() -> None:
        await blocker.wait()

    assistant._start_background(wait_for_blocker())
    await assistant.close()
    assert not cast(Any, assistant)._background_tasks

    clear = AsyncMock()
    warn = AsyncMock()
    cast(Any, assistant)._clear_missing_tag_warning = clear
    cast(Any, assistant)._warn_missing_tag = warn
    await assistant.on_ready()
    clear.assert_awaited_once()
    await assistant.on_error("on_disconnect")

    taxonomy.refresh_result = True
    cast(Any, assistant).is_ready = lambda: True
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError])
    cast(Any, taxonomy).refresh = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr("paperless_assistant.discord_adapter.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await assistant._taxonomy_loop()
    assert ready[-2:] == [True, False]
    clear.assert_awaited()
    warn.assert_awaited_once()

    recover = AsyncMock()
    cast(Any, ingestion).recover = recover
    sleep.side_effect = [None, asyncio.CancelledError]
    with pytest.raises(asyncio.CancelledError):
        await assistant._recovery_loop()
    recover.assert_awaited_once()


@pytest.mark.asyncio
async def test_success_review_initial_and_recovery_render_once(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    job = replace(
        _job(tmp_path, JobState.SUCCEEDED),
        discord_message_id=70,
        discord_attachment_id=71,
        principal_id=201,
        paperless_document_id=DocumentId(44),
    )
    await ingestion.create_upload_batch(
        UploadBatch(70, channel.id, 72, channel.id, 201, 1),
        (
            UploadItem(
                70,
                71,
                1,
                "simultaneous.pdf",
                state=UploadItemState.SUCCEEDED,
                job_id=job.id,
                document_id=DocumentId(44),
            ),
        ),
    )
    outcome = IngestionOutcome(
        job,
        Document(DocumentId(44), "Simultaneous", date(2026, 7, 28)),
    )

    def lookup(identifier: int) -> Any:
        if identifier == channel.id:
            return channel
        return next(
            (
                message.thread
                for message in channel.sent
                if hasattr(message, "thread") and message.thread.id == identifier
            ),
            None,
        )

    monkeypatch.setattr(assistant, "get_channel", lookup)
    source = FakeMessage(
        channel=channel,
        identifier=70,
        attachments=(FakeAttachment(71, "simultaneous.pdf", b"%PDF-1.7"),),
    )
    batch_controller = assistant._upload_batch_controller(70, 201)
    await asyncio.gather(
        assistant._create_success_upload_review(
            channel=channel,
            message=cast(discord.Message, source),
            outcome=outcome,
            ordinal=1,
            total_items=1,
            batch_controller=batch_controller,
        ),
        assistant._notify_upload_item_recovery(outcome, ingestion.upload_items[71]),
    )

    parents = [message for message in channel.sent if hasattr(message, "thread")]
    assert len(parents) == 1
    assert len(parents[0].thread.sent) == 4
    assert len(batch_controller.controllers) == 1
    saved = ingestion.upload_items[71]
    assert {
        saved.title_message_id,
        saved.metadata_message_id,
        saved.actions_message_id,
        saved.controls_message_id,
    } == {1000, 1001, 1002, 1003}
    assert not await assistant._create_success_upload_review(
        channel=channel,
        message=cast(discord.Message, source),
        outcome=outcome,
        ordinal=1,
        total_items=1,
        batch_controller=batch_controller,
    )
    assert not await assistant._create_success_upload_review_locked(
        channel=channel,
        message=cast(discord.Message, source),
        outcome=IngestionOutcome(job),
        ordinal=1,
        total_items=1,
        batch_controller=batch_controller,
    )

    missing = FakeMessage(channel=cast(Any, parents[0].thread), identifier=9000)
    missing.edit = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.NotFound(
            cast(Any, SimpleNamespace(status=404, reason="Not Found")),
            "synthetic",
        )
    )
    cast(Any, parents[0].thread).get_partial_message = lambda _: missing
    replacement = await AISuggestionsView._send_or_edit(
        parents[0].thread,
        9000,
        "replacement",
        discord.ui.View(),
    )
    assert cast(Any, replacement).content == "replacement"

    await assistant._restore_batch_summary(
        UploadBatchSnapshot(
            UploadBatch(70, channel.id, 72, channel.id, 201, 1),
            (saved,),
        ),
        batch_controller,
    )
    monkeypatch.setattr(assistant, "get_channel", lambda _: None)
    await assistant._restore_batch_summary(
        UploadBatchSnapshot(
            UploadBatch(70, channel.id, 72, channel.id, 201, 1),
            (saved,),
        ),
        batch_controller,
    )


@pytest.mark.asyncio
async def test_upload_clean_removes_only_safe_bot_owned_orphans(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(discord.Client, "user", property(lambda _: SimpleNamespace(id=999)))
    channel = FakeChannel(settings.discord_uploads_channel_id)

    orphan_parent = FakeMessage(
        channel=channel,
        content="**Document 1/2 · orphan.pdf**\n**Status:** Review ready",
        identifier=501,
        user_id=999,
    )
    tracked_parent = FakeMessage(
        channel=channel,
        content="**Document 2/2 · tracked.pdf**\n**Status:** Review ready",
        identifier=502,
        user_id=999,
    )
    user_parent = FakeMessage(
        channel=channel,
        content="**Document 3/3 · user.pdf**\n**Status:** Review ready",
        identifier=503,
        user_id=201,
    )
    pinned_parent = FakeMessage(
        channel=channel,
        content="**Document 4/4 · pinned.pdf**\n**Status:** Review ready",
        identifier=504,
        user_id=999,
    )
    pinned_parent.pinned = True
    failed_parent = FakeMessage(
        channel=channel,
        content="**Document 5/5 · failed.pdf**\n**Status:** Review ready",
        identifier=505,
        user_id=999,
    )
    failed_parent.delete = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.HTTPException(
            cast(Any, SimpleNamespace(status=403, reason="Forbidden")),
            "synthetic",
        )
    )

    async def parent_history(_: int) -> Any:
        for message in (
            orphan_parent,
            tracked_parent,
            user_parent,
            pinned_parent,
            failed_parent,
        ):
            yield message

    channel.history_factory = parent_history
    orphan_thread = FakeThread(channel.id, 601, "Document 1/2: orphan.pdf")
    tracked_thread = FakeThread(channel.id, 602, "Document 2/2: tracked.pdf")
    wrong_owner = FakeThread(channel.id, 603, "Document 3/3: wrong-owner.pdf")
    wrong_owner.owner_id = 201
    wrong_name = FakeThread(channel.id, 604, "Unrelated")
    failed_thread = FakeThread(channel.id, 605, "Document 5/5: failed.pdf")
    failed_thread.delete = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.HTTPException(
            cast(Any, SimpleNamespace(status=403, reason="Forbidden")),
            "synthetic",
        )
    )
    archived_orphan = FakeThread(channel.id, 606, "Document 6/6: archived.pdf")
    channel.threads = [
        orphan_thread,
        tracked_thread,
        wrong_owner,
        wrong_name,
        failed_thread,
    ]
    channel.archived = [archived_orphan]

    canonical = FakeMessage(
        channel=cast(Any, tracked_thread),
        content="**Title**\nTracked",
        identifier=701,
        user_id=999,
    )
    duplicate = FakeMessage(
        channel=cast(Any, tracked_thread),
        content="**Editable Metadata**\nDuplicate",
        identifier=702,
        user_id=999,
    )
    user_duplicate = FakeMessage(
        channel=cast(Any, tracked_thread),
        content="**Editable Metadata**\nUser",
        identifier=703,
        user_id=201,
    )
    unrelated = FakeMessage(
        channel=cast(Any, tracked_thread),
        content="Ordinary bot note",
        identifier=704,
        user_id=999,
    )
    failed_duplicate = FakeMessage(
        channel=cast(Any, tracked_thread),
        content="Recovered document review.",
        identifier=705,
        user_id=999,
    )
    failed_duplicate.delete = AsyncMock(  # type: ignore[method-assign]
        side_effect=discord.HTTPException(
            cast(Any, SimpleNamespace(status=403, reason="Forbidden")),
            "synthetic",
        )
    )
    tracked_thread.sent = [
        canonical,
        duplicate,
        user_duplicate,
        unrelated,
        failed_duplicate,
    ]
    tracked = (
        UploadItem(
            1,
            1,
            1,
            "tracked.pdf",
            state=UploadItemState.SUCCEEDED,
            parent_message_id=502,
            thread_id=602,
            title_message_id=701,
        ),
        UploadItem(2, 2, 1, "closed.pdf", state=UploadItemState.CLOSED, thread_id=700),
        UploadItem(3, 3, 1, "no-thread.pdf", state=UploadItemState.SUCCEEDED),
        UploadItem(
            4,
            4,
            1,
            "no-ids.pdf",
            state=UploadItemState.SUCCEEDED,
            thread_id=800,
        ),
        UploadItem(
            5,
            5,
            1,
            "missing-thread.pdf",
            state=UploadItemState.SUCCEEDED,
            thread_id=801,
            controls_message_id=802,
        ),
    )
    monkeypatch.setattr(
        assistant,
        "get_channel",
        lambda identifier: tracked_thread if identifier == 602 else None,
    )
    cleaned = await assistant._clean_upload_orphans(
        cast(discord.TextChannel, channel),
        100,
        tracked,
    )
    assert cleaned == 4
    assert orphan_parent.deleted
    assert orphan_thread.deleted
    assert archived_orphan.deleted
    assert duplicate.deleted
    assert not tracked_parent.deleted
    assert not canonical.deleted
    assert not user_duplicate.deleted
    assert not unrelated.deleted

    transport_error = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="Synthetic")),
        "synthetic",
    )
    error_channel = FakeChannel(settings.discord_uploads_channel_id)

    async def failed_history(_: int) -> Any:
        raise transport_error
        yield None

    async def failed_archived(*, limit: int) -> Any:
        del limit
        raise transport_error
        yield None

    error_channel.history_factory = failed_history
    error_channel.archived_threads = failed_archived  # type: ignore[assignment]
    history_error_thread = FakeThread(
        error_channel.id,
        901,
        "Document 1/1: history-error.pdf",
    )

    async def thread_history_error(limit: int) -> Any:
        del limit
        raise transport_error
        yield None

    history_error_thread.history = thread_history_error  # type: ignore[assignment]
    monkeypatch.setattr(
        assistant,
        "get_channel",
        lambda identifier: history_error_thread if identifier == 901 else None,
    )
    assert (
        await assistant._clean_upload_orphans(
            cast(discord.TextChannel, error_channel),
            10,
            (
                UploadItem(
                    9,
                    9,
                    1,
                    "history-error.pdf",
                    state=UploadItemState.SUCCEEDED,
                    thread_id=901,
                    controls_message_id=902,
                ),
            ),
        )
        == 0
    )


@pytest.mark.asyncio
async def test_resolved_upload_cleanup_retries_partial_discord_failures(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    not_found = discord.NotFound(
        cast(Any, SimpleNamespace(status=404, reason="Not Found")),
        "synthetic",
    )
    transport_error = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="Synthetic")),
        "synthetic",
    )

    async def exercise(
        attachment_id: int,
        *,
        cached: Any = None,
        fetched: Any = None,
        guild: bool = True,
    ) -> UploadItem:
        item = UploadItem(
            800,
            attachment_id,
            1,
            "resolved.pdf",
            state=UploadItemState.CLOSED,
            thread_id=900 + attachment_id,
        )
        await ingestion.create_upload_batch(
            UploadBatch(800, 102, 850, 102, 201, 1),
            (item,),
        )
        monkeypatch.setattr(assistant, "get_channel", lambda _: cached)
        fetch = AsyncMock()
        if isinstance(fetched, BaseException):
            fetch.side_effect = fetched
        else:
            fetch.return_value = fetched
        monkeypatch.setattr(
            assistant,
            "get_guild",
            lambda _: SimpleNamespace(fetch_channel=fetch) if guild else None,
        )
        await assistant._cleanup_resolved_upload_item(800, attachment_id, ())
        return ingestion.upload_items[attachment_id]

    fetched_thread = FakeThread(102, 901)
    assert (await exercise(1, fetched=fetched_thread)).thread_cleaned
    assert (await exercise(2, fetched=not_found)).thread_cleaned
    assert not (await exercise(3, fetched=transport_error)).thread_cleaned
    assert not (await exercise(4, fetched=SimpleNamespace())).thread_cleaned
    assert not (await exercise(5, guild=False)).thread_cleaned

    missing_thread = FakeThread(102, 906)
    missing_thread.delete = AsyncMock(side_effect=not_found)  # type: ignore[method-assign]
    assert (await exercise(6, cached=missing_thread)).thread_cleaned
    failed_thread = FakeThread(102, 907)
    failed_thread.delete = AsyncMock(  # type: ignore[method-assign]
        side_effect=transport_error
    )
    assert not (await exercise(7, cached=failed_thread)).thread_cleaned

    parent_only = UploadItem(
        810,
        8,
        1,
        "parent.pdf",
        state=UploadItemState.DISMISSED,
        parent_message_id=811,
        parent_channel_id=102,
    )
    await ingestion.create_upload_batch(
        UploadBatch(810, 102, 812, 102, 201, 1),
        (parent_only,),
    )
    assistant.cleanup_messages = AsyncMock(return_value=())  # type: ignore[method-assign]
    await assistant._cleanup_resolved_upload_item(810, 8, ())
    assert not ingestion.upload_items[8].parent_cleaned

    assistant._cleanup_upload_targets = AsyncMock()  # type: ignore[method-assign]
    shared = (DiscordMessageTarget(102, 999),)
    await assistant._cleanup_resolved_upload_item(999, 999, shared)
    assistant._cleanup_upload_targets.assert_awaited_once_with(shared)

    assistant._cleanup_resolved_upload_item = AsyncMock()  # type: ignore[method-assign]
    mismatch = _ReviewThreadController(
        201,
        900,
        source_message_id=820,
        attachment_id=999,
    )
    controller = _UploadBatchController(201, 900)
    controller.add(mismatch)
    assistant._upload_batch_controllers[820] = controller
    await assistant.close_upload_items(
        (
            UploadItem(820, 821, 1, "mismatch.pdf", state=UploadItemState.CLOSED),
            UploadItem(830, 831, 1, "unbound.pdf", state=UploadItemState.CLOSED),
        )
    )
    assert not mismatch.closed


@pytest.mark.asyncio
async def test_recovery_warning_and_cleanup_edge_paths(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    channel = FakeChannel(settings.discord_uploads_channel_id)
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    cast(Any, assistant).get_channel = lambda _: channel

    succeeded = _job(tmp_path, JobState.SUCCEEDED)
    cast(Any, ingestion).message_succeeded = AsyncMock(return_value=False)
    await assistant._notify_recovery(
        IngestionOutcome(
            succeeded,
            Document(DocumentId(44), "Recovered", date(2024, 1, 1)),
            note_failed=True,
        )
    )
    assert "Guidance note failed" in channel.sent[-1].content

    await assistant._notify_recovery(
        IngestionOutcome(_job(tmp_path, JobState.RECONCILIATION_REQUIRED))
    )
    assert "uncertain" in channel.sent[-1].content
    await assistant._notify_recovery(
        IngestionOutcome(
            _job(tmp_path, JobState.FAILED, office_dependent=True),
        )
    )
    assert "Tika/Gotenberg" in channel.sent[-1].content
    assert "file corrupted" not in channel.sent[-1].content

    not_found = discord.NotFound(
        cast(Any, SimpleNamespace(status=404, reason="synthetic")),
        "synthetic",
    )
    http_error = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="synthetic")),
        "synthetic",
    )
    cast(Any, assistant).get_channel = lambda _: None
    await assistant.cleanup_messages(
        (DiscordMessageTarget(settings.discord_questions_channel_id, 1),),
        (DiscordMessageTarget(settings.discord_uploads_channel_id, 2),),
    )

    cleanup_targets = tuple(
        DiscordMessageTarget(settings.discord_uploads_channel_id, value) for value in (10, 11, 12)
    )
    guild = SimpleNamespace(fetch_channel=AsyncMock(side_effect=not_found))
    cast(Any, assistant).get_guild = lambda _: guild
    assert await assistant.cleanup_messages(cleanup_targets, ()) == cleanup_targets

    guild.fetch_channel = AsyncMock(side_effect=http_error)
    assert await assistant.cleanup_messages(cleanup_targets, ()) == ()
    guild.fetch_channel = AsyncMock(return_value=SimpleNamespace())
    assert await assistant.cleanup_messages(cleanup_targets, ()) == ()

    deletes = (
        AsyncMock(side_effect=not_found),
        AsyncMock(side_effect=http_error),
        AsyncMock(),
    )
    original_partial_message = channel.get_partial_message
    delete_iterator = iter(deletes)
    cast(Any, channel).get_partial_message = lambda _identifier: SimpleNamespace(
        delete=next(delete_iterator)
    )
    guild.fetch_channel = AsyncMock(return_value=channel)
    assert await assistant.cleanup_messages(cleanup_targets, ()) == (
        cleanup_targets[0],
        cleanup_targets[2],
    )
    cast(Any, channel).get_partial_message = original_partial_message
    await assistant._warn_missing_tag()

    ingestion.warning_marker = None
    await assistant._clear_missing_tag_warning()
    ingestion.warning_marker = (10, datetime.now(tz=UTC) - timedelta(days=2))
    await assistant._clear_missing_tag_warning()
    assert ingestion.warning_marker is not None

    cast(Any, assistant).get_channel = lambda _: channel
    await assistant._warn_missing_tag()
    assert channel.partial[-1].deleted

    ingestion.warning_marker = (20, datetime.now(tz=UTC))
    cast(Any, channel).get_partial_message = lambda identifier: cast(
        Any,
        SimpleNamespace(delete=AsyncMock(side_effect=not_found)),
    )
    await assistant._clear_missing_tag_warning()
    assert ingestion.warning_marker is None

    ingestion.warning_marker = (21, datetime.now(tz=UTC))
    cast(Any, channel).get_partial_message = lambda identifier: cast(
        Any,
        SimpleNamespace(delete=AsyncMock(side_effect=http_error)),
    )
    await assistant._clear_missing_tag_warning()
    assert ingestion.warning_marker is not None

    settings.staging_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "iterdir", lambda _: (_ for _ in ()).throw(OSError()))
    assert assistant._staging_usage() == settings.ingestion_max_staged_bytes
    await assistant.close()


@pytest.mark.asyncio
async def test_gateway_lifecycle_hooks(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    taxonomy = FakeTaxonomy(False)
    ready: list[bool] = []
    assistant = DiscordAssistant(
        settings,
        cast(Any, FakeQuery()),
        cast(Any, ingestion),
        cast(Any, FakeDelivery(tmp_path)),
        cast(Any, taxonomy),
        ready_callback=ready.append,
    )
    started: list[Any] = []

    def capture(coroutine: Any) -> None:
        started.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(assistant, "_start_background", capture)
    await assistant.setup_hook()
    assert ingestion.recovered
    assert len(started) == 2

    channel = FakeChannel(settings.discord_uploads_channel_id)
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
    monkeypatch.setattr(assistant, "get_channel", lambda _: channel)
    await assistant.on_ready()
    assert ready[-1] is False
    await assistant.on_disconnect()
    assert ready[-1] is False
    await assistant.close()


@pytest.mark.asyncio
async def test_upload_dismiss_button_callback(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    button = UploadDismissButton(settings.discord_allowed_user_ids)
    msg = FakeMessage(channel=FakeChannel(settings.discord_questions_channel_id))
    interaction_allowed = AsyncMock()
    interaction_allowed.user = SimpleNamespace(id=201)
    interaction_allowed.message = msg
    await button.callback(cast(discord.Interaction, interaction_allowed))
    interaction_allowed.message = None
    await button.callback(cast(discord.Interaction, interaction_allowed))

    interaction_unauthorized = AsyncMock()
    interaction_unauthorized.user = SimpleNamespace(id=999)
    await button.callback(cast(discord.Interaction, interaction_unauthorized))
    interaction_unauthorized.response.send_message.assert_awaited_with(
        "You are not authorized to dismiss this message.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_clean_command_callback(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    ingestion = FakeIngestion()
    assistant = _assistant(
        settings,
        FakeQuery(),
        ingestion,
        FakeDelivery(tmp_path),
        FakeTaxonomy(),
    )
    monkeypatch.setattr(discord, "TextChannel", FakeChannel)

    clean_cmd = assistant.tree.get_command("clean")
    assert isinstance(clean_cmd, discord.app_commands.Command)
    callback = cast(Any, clean_cmd.callback)

    interaction_clean = AsyncMock()
    interaction_clean.user = SimpleNamespace(id=999)
    await callback(cast(discord.Interaction, interaction_clean), 10)
    interaction_clean.response.send_message.assert_awaited_with(
        "You are not authorized to run this command.", ephemeral=True
    )

    interaction_wrong_channel = AsyncMock()
    interaction_wrong_channel.user = SimpleNamespace(id=201)
    interaction_wrong_channel.channel_id = 999
    await callback(cast(discord.Interaction, interaction_wrong_channel), 10)
    interaction_wrong_channel.response.send_message.assert_awaited_with(
        "Clean command can only be used in assistant channels.", ephemeral=True
    )

    await ingestion.create_upload_batch(
        UploadBatch(500, settings.discord_uploads_channel_id, 600, 102, 201, 1),
        (
            UploadItem(
                500,
                1,
                1,
                "done.pdf",
                state=UploadItemState.CLOSED,
                parent_message_id=700,
                parent_channel_id=102,
            ),
        ),
    )
    tracked = (
        DiscordMessageTarget(102, 600),
        DiscordMessageTarget(settings.discord_uploads_channel_id, 500),
    )
    assistant.cleanup_messages = AsyncMock(return_value=tracked)  # type: ignore[method-assign]
    assistant._cleanup_resolved_upload_item = AsyncMock()  # type: ignore[method-assign]
    interaction_uploads = AsyncMock(
        user=SimpleNamespace(id=201),
        channel_id=settings.discord_uploads_channel_id,
    )
    interaction_uploads.channel = FakeChannel(settings.discord_uploads_channel_id)
    await callback(cast(discord.Interaction, interaction_uploads), 10)
    assistant._cleanup_resolved_upload_item.assert_awaited_once_with(500, 1, ())
    assistant.cleanup_messages.assert_awaited_once_with((), tracked)
    assert ingestion.confirmed_cleanup == list(tracked)
    assistant.cleanup_messages = AsyncMock(return_value=())  # type: ignore[method-assign]
    await callback(cast(discord.Interaction, interaction_uploads), 10)

    invalid_upload = AsyncMock(
        user=SimpleNamespace(id=201),
        channel_id=settings.discord_uploads_channel_id,
        channel="invalid",
    )
    await callback(cast(discord.Interaction, invalid_upload), 10)
    invalid_upload.followup.send.assert_awaited_with("Invalid channel type.", ephemeral=True)

    interaction_non_text = AsyncMock()
    interaction_non_text.user = SimpleNamespace(id=201)
    interaction_non_text.channel_id = settings.discord_questions_channel_id
    interaction_non_text.channel = "invalid"
    await callback(cast(discord.Interaction, interaction_non_text), 10)
    interaction_non_text.followup.send.assert_awaited_with("Invalid channel type.", ephemeral=True)

    channel = FakeChannel(settings.discord_questions_channel_id)
    bot_msg = FakeMessage(channel=channel, user_id=123)
    user_msg = FakeMessage(channel=channel, user_id=999)
    pinned_msg = FakeMessage(channel=channel, user_id=999)
    pinned_msg.pinned = True
    channel.sent.extend([bot_msg, user_msg, pinned_msg])
    monkeypatch.setattr(discord.Client, "user", property(lambda s: SimpleNamespace(id=123)))

    class FakeHistory:
        def __aiter__(self) -> FakeHistory:
            self._messages = [bot_msg, user_msg, pinned_msg]
            self._idx = 0
            return self

        async def __anext__(self) -> FakeMessage:
            if self._idx < len(self._messages):
                msg = self._messages[self._idx]
                self._idx += 1
                return msg
            raise StopAsyncIteration

    channel.history_factory = lambda limit=100: FakeHistory()

    interaction_valid = AsyncMock()
    interaction_valid.user = SimpleNamespace(id=201)
    interaction_valid.channel_id = settings.discord_questions_channel_id
    interaction_valid.channel = channel
    interaction_valid.client = SimpleNamespace(user=SimpleNamespace(id=123))
    await callback(cast(discord.Interaction, interaction_valid), 10)
    assert bot_msg.deleted
    assert user_msg.deleted
    assert not pinned_msg.deleted
    interaction_valid.followup.send.assert_awaited_with("Cleaned 2 message(s).", ephemeral=True)


@pytest.mark.asyncio
async def test_thread_routing_and_render_query_error_handling(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.staging_dir.mkdir(parents=True)
    query = FakeQuery()
    delivery = FakeDelivery(tmp_path)
    assistant = _assistant(settings, query, FakeIngestion(), delivery, FakeTaxonomy())

    q_thread = FakeThread(parent_id=settings.discord_questions_channel_id, thread_id=5001)
    q_thread_msg = FakeMessage(channel=cast(Any, q_thread), content="Followup in thread")
    assert assistant._authorized_message(cast(discord.Message, q_thread_msg))

    unauth_thread = FakeThread(parent_id=999, thread_id=5002)
    unauth_thread_msg = FakeMessage(channel=cast(Any, unauth_thread), content="Forbidden")
    assert not assistant._authorized_message(cast(discord.Message, unauth_thread_msg))

    await assistant.on_message(cast(discord.Message, q_thread_msg))
    assert q_thread.sent[0].content == "Native answer"

    u_thread = FakeThread(parent_id=settings.discord_uploads_channel_id, thread_id=5003)
    u_thread_msg = FakeMessage(
        channel=cast(Any, u_thread),
        attachments=[FakeAttachment(1, "doc.pdf", b"%PDF-1.7")],
    )
    await assistant.on_message(cast(discord.Message, u_thread_msg))
    assert (
        "uploaded as"
        in _latest_upload_summary(cast(FakeChannel, u_thread.parent)).edits[-1]["content"]
    )

    delivery.mode = "link"
    status_msg_link = FakeMessage(channel=cast(Any, q_thread), content="status")
    await assistant._render_query(
        cast(discord.Message, status_msg_link),
        query.response,
        principal_id=201,
        context_id=5001,
    )

    delivery.mode = "error"
    status_msg_err = FakeMessage(channel=cast(Any, q_thread), content="status")
    await assistant._render_query(
        cast(discord.Message, status_msg_err),
        query.response,
        principal_id=201,
        context_id=5001,
    )
    assert query.saved is not None
    assert query.saved[0] == 5001

    dummy_msg = FakeMessage(channel=FakeChannel(settings.discord_questions_channel_id))
    delivery.mode = "link"
    await assistant._deliver_to_message(cast(discord.Message, dummy_msg), (7,), target=q_thread)
    assert "too large" in q_thread.sent[-1].content

    delivery.mode = "error"
    await assistant._deliver_to_message(cast(discord.Message, dummy_msg), (7,), target=q_thread)
    assert "unavailable" in q_thread.sent[-1].content

    malformed = SimpleNamespace(
        data={"custom_id": "paperless:send:invalid"},
        guild_id=settings.discord_guild_id,
        user=SimpleNamespace(id=201),
    )
    await assistant.on_interaction(cast(discord.Interaction, malformed))
    await assistant.close()


@pytest.mark.asyncio
async def test_auth_link_and_unlink_commands(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    settings.discord_allowed_user_ids = frozenset({9999})

    class FakeCredentials:
        def __init__(self) -> None:
            self.tokens: dict[int, SecretStr] = {}

        async def save_user_token(self, user_id: int, token: SecretStr) -> None:
            self.tokens[user_id] = token

        async def get_user_token(self, user_id: int) -> SecretStr | None:
            return self.tokens.get(user_id)

        async def delete_user_token(self, user_id: int) -> bool:
            return self.tokens.pop(user_id, None) is not None

    class FakeGateway:
        async def validate_token(self, token: SecretStr) -> bool:
            return token.get_secret_value() == "valid-token"

    creds = FakeCredentials()
    gateway = FakeGateway()
    assistant = DiscordAssistant(
        settings,
        cast(Any, FakeQuery()),
        cast(Any, FakeIngestion()),
        cast(Any, FakeDelivery(tmp_path)),
        cast(Any, FakeTaxonomy(ready=True)),
        credentials=creds,
        paperless_gateway=cast(Any, gateway),
        ready_callback=lambda _: None,
    )

    auth_group = assistant.tree.get_command("auth")
    assert isinstance(auth_group, discord.app_commands.Group)
    link_cmd = auth_group.get_command("link")
    unlink_cmd = auth_group.get_command("unlink")
    assert isinstance(link_cmd, discord.app_commands.Command)
    assert isinstance(unlink_cmd, discord.app_commands.Command)

    # Invalid link
    interaction_invalid = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, link_cmd.callback)(
        cast(discord.Interaction, interaction_invalid),
        token="bad-token",  # noqa: S106
    )
    assert "rejected" in interaction_invalid.followup.send.call_args[0][0]

    # Valid link
    interaction_valid = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, link_cmd.callback)(
        cast(discord.Interaction, interaction_valid),
        token="valid-token",  # noqa: S106
    )
    assert "securely linked" in interaction_valid.followup.send.call_args[0][0]
    assert creds.tokens[9999].get_secret_value() == "valid-token"

    # Unlink
    interaction_unlink = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, unlink_cmd.callback)(cast(discord.Interaction, interaction_unlink))
    assert "removed" in interaction_unlink.followup.send.call_args[0][0]

    # Unlink when empty
    interaction_unlink_again = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, unlink_cmd.callback)(cast(discord.Interaction, interaction_unlink_again))
    assert "No linked Paperless" in interaction_unlink_again.followup.send.call_args[0][0]
    await assistant.close()

    gateway_only = DiscordAssistant(
        settings,
        cast(Any, FakeQuery()),
        cast(Any, FakeIngestion()),
        cast(Any, FakeDelivery(tmp_path)),
        cast(Any, FakeTaxonomy(ready=True)),
        paperless_gateway=cast(Any, gateway),
        ready_callback=lambda _: None,
    )
    gateway_only_group = gateway_only.tree.get_command("auth")
    assert isinstance(gateway_only_group, discord.app_commands.Group)
    gateway_only_link = gateway_only_group.get_command("link")
    gateway_only_unlink = gateway_only_group.get_command("unlink")
    assert isinstance(gateway_only_link, discord.app_commands.Command)
    assert isinstance(gateway_only_unlink, discord.app_commands.Command)
    link_without_store = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, gateway_only_link.callback)(
        cast(discord.Interaction, link_without_store),
        token="valid-token",  # noqa: S106
    )
    assert "securely linked" in link_without_store.followup.send.call_args.args[0]
    unlink_without_store = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, gateway_only_unlink.callback)(cast(discord.Interaction, unlink_without_store))
    assert "No linked" in unlink_without_store.followup.send.call_args.args[0]
    await gateway_only.close()


@pytest.mark.asyncio
async def test_auth_status_command(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.discord_allowed_user_ids = frozenset({9999})

    class FakeCredentials:
        def __init__(self) -> None:
            self.tokens: dict[int, SecretStr] = {}

        async def save_user_token(self, user_id: int, token: SecretStr) -> None:
            self.tokens[user_id] = token

        async def get_user_token(self, user_id: int) -> SecretStr | None:
            return self.tokens.get(user_id)

        async def delete_user_token(self, user_id: int) -> bool:
            return False

    class FakeGateway:
        async def validate_token(self, token: SecretStr) -> bool:
            return token.get_secret_value() == "valid-token"

    creds = FakeCredentials()
    gateway = FakeGateway()
    assistant = DiscordAssistant(
        settings,
        cast(Any, FakeQuery()),
        cast(Any, FakeIngestion()),
        cast(Any, FakeDelivery(tmp_path)),
        cast(Any, FakeTaxonomy(ready=True)),
        credentials=creds,
        paperless_gateway=cast(Any, gateway),
        ready_callback=lambda _: None,
    )

    auth_group = assistant.tree.get_command("auth")
    assert isinstance(auth_group, discord.app_commands.Group)
    status_cmd = auth_group.get_command("status")
    assert isinstance(status_cmd, discord.app_commands.Command)

    # Status when unlinked
    interaction_unlinked = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, status_cmd.callback)(cast(discord.Interaction, interaction_unlinked))
    assert "have not linked" in interaction_unlinked.followup.send.call_args[0][0]

    # Status when active
    creds.tokens[9999] = SecretStr("valid-token")
    interaction_active = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, status_cmd.callback)(cast(discord.Interaction, interaction_active))
    assert "linked and active" in interaction_active.followup.send.call_args[0][0]

    # Status when revoked
    creds.tokens[9999] = SecretStr("revoked-token")
    interaction_revoked = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, status_cmd.callback)(cast(discord.Interaction, interaction_revoked))
    assert "rejected by Paperless" in interaction_revoked.followup.send.call_args[0][0]
    await assistant.close()

    credentials_only = DiscordAssistant(
        settings,
        cast(Any, FakeQuery()),
        cast(Any, FakeIngestion()),
        cast(Any, FakeDelivery(tmp_path)),
        cast(Any, FakeTaxonomy(ready=True)),
        credentials=creds,
        ready_callback=lambda _: None,
    )
    credentials_only_group = credentials_only.tree.get_command("auth")
    assert isinstance(credentials_only_group, discord.app_commands.Group)
    credentials_only_status = credentials_only_group.get_command("status")
    assert isinstance(credentials_only_status, discord.app_commands.Command)
    without_gateway = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, credentials_only_status.callback)(cast(discord.Interaction, without_gateway))
    assert "rejected by Paperless" in without_gateway.followup.send.call_args.args[0]
    await credentials_only.close()


@pytest.mark.asyncio
async def test_auth_commands_unauthorized_user(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.discord_allowed_user_ids = frozenset({201})

    assistant = DiscordAssistant(
        settings,
        cast(Any, FakeQuery()),
        cast(Any, FakeIngestion()),
        cast(Any, FakeDelivery(tmp_path)),
        cast(Any, FakeTaxonomy(ready=True)),
        ready_callback=lambda _: None,
    )

    assert assistant._authorized_user_id(9999) is False

    auth_group = assistant.tree.get_command("auth")
    assert isinstance(auth_group, discord.app_commands.Group)
    link_cmd = auth_group.get_command("link")
    unlink_cmd = auth_group.get_command("unlink")
    status_cmd = auth_group.get_command("status")
    clean_cmd = assistant.tree.get_command("clean")
    assert isinstance(link_cmd, discord.app_commands.Command)
    assert isinstance(unlink_cmd, discord.app_commands.Command)
    assert isinstance(status_cmd, discord.app_commands.Command)
    assert isinstance(clean_cmd, discord.app_commands.Command)

    no_gateway_link = AsyncMock(user=SimpleNamespace(id=201))
    await cast(Any, link_cmd.callback)(
        cast(discord.Interaction, no_gateway_link),
        token="synthetic",  # noqa: S106
    )
    assert "rejected" in no_gateway_link.followup.send.call_args.args[0]

    interaction_unauth = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, clean_cmd.callback)(cast(discord.Interaction, interaction_unauth), 10)
    assert "not authorized" in interaction_unauth.response.send_message.call_args[0][0]

    interaction_unauth_link = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, link_cmd.callback)(
        cast(discord.Interaction, interaction_unauth_link),
        token="valid-token",  # noqa: S106
    )
    assert "not authorized" in interaction_unauth_link.response.send_message.call_args[0][0]

    interaction_unauth_unlink = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, unlink_cmd.callback)(cast(discord.Interaction, interaction_unauth_unlink))
    assert "not authorized" in interaction_unauth_unlink.response.send_message.call_args[0][0]

    interaction_unauth_status = AsyncMock(user=SimpleNamespace(id=9999))
    await cast(Any, status_cmd.callback)(cast(discord.Interaction, interaction_unauth_status))
    assert "not authorized" in interaction_unauth_status.response.send_message.call_args[0][0]
    await assistant.close()


@pytest.mark.asyncio
async def test_unlinked_user_responses(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    query = FakeQuery()
    ingestion = FakeIngestion()
    delivery = FakeDelivery(tmp_path)
    taxonomy = FakeTaxonomy(ready=True)

    assistant = DiscordAssistant(
        settings,
        cast(Any, query),
        cast(Any, ingestion),
        cast(Any, delivery),
        cast(Any, taxonomy),
        ready_callback=lambda _: None,
    )

    async def raise_unlinked(*args: Any, **kwargs: Any) -> Any:
        raise UnlinkedUserError("unlinked")

    q_channel = FakeChannel(settings.discord_questions_channel_id)
    msg_unlinked_q = FakeMessage(channel=q_channel, content="What is this?", user_id=201)
    query.ask = raise_unlinked  # type: ignore[method-assign]
    await assistant.on_message(cast(discord.Message, msg_unlinked_q))
    assert "have not linked your Paperless account" in msg_unlinked_q.thread.sent[0].content

    u_channel = FakeChannel(settings.discord_uploads_channel_id)
    msg_unlinked_u = FakeMessage(
        channel=u_channel,
        attachments=[FakeAttachment(1, "doc.pdf", b"%PDF-1.7")],
        user_id=201,
    )
    ingestion.submit = raise_unlinked  # type: ignore[method-assign]
    await assistant.on_message(cast(discord.Message, msg_unlinked_u))
    assert "linked Paperless account" in _latest_upload_summary(u_channel).edits[-1]["content"]

    interaction_button = AsyncMock()
    interaction_button.guild_id = settings.discord_guild_id
    interaction_button.user = SimpleNamespace(id=201)
    interaction_button.channel = q_channel
    interaction_button.channel_id = settings.discord_questions_channel_id
    interaction_button.data = {"custom_id": "paperless:send:201:7"}
    delivery.prepare = raise_unlinked  # type: ignore[method-assign]
    query.context = AsyncMock(return_value=SimpleNamespace(document_ids=(7,)))  # type: ignore[method-assign]
    await assistant.on_interaction(cast(discord.Interaction, interaction_button))
    assert (
        "have not linked your Paperless account" in interaction_button.followup.send.call_args[0][0]
    )

    await assistant.close()


def _ai_review_fixture(tmp_path: Path) -> tuple[IngestionJob, SuggestionReview, FakeIngestion]:
    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        principal_id=201,
        staged_path=tmp_path / "synthetic.pdf",
        original_filename="synthetic.pdf",
        caption="",
        media_type="application/pdf",
        office_dependent=False,
        guidance=MetadataGuidance(),
    )
    taxonomy = Taxonomy(
        tags=(
            TaxonomyItem(2, "Existing Tag"),
            TaxonomyItem(8, "New Tag Zero"),
        ),
        correspondents=(
            TaxonomyItem(1, "Mary"),
            TaxonomyItem(9, "Mary Smith"),
        ),
        document_types=(TaxonomyItem(3, "Chat Log"),),
        storage_paths=(TaxonomyItem(4, "Personal/Chat Logs"),),
    )
    review = SuggestionReview(
        Document(
            DocumentId(7),
            "Current",
            date(2026, 7, 1),
            modified=datetime(2026, 7, 28, tzinfo=UTC),
            tag_ids=(2,),
            correspondent_id=1,
            document_type_id=3,
            storage_path_id=4,
        ),
        AISuggestions(
            title="Chat Conversation with Mary",
            dates=(
                SuggestedDate("2026-07-28", date(2026, 7, 28)),
                SuggestedDate("invalid", None),
            ),
            correspondent_ids=(1,),
            document_type_ids=(3,),
            storage_path_ids=(4,),
            tag_ids=(2,),
            suggested_correspondents=("Mary Smyth",),
            suggested_document_types=("Text Message",),
            suggested_storage_paths=("Messages/Mary",),
            suggested_tags=("New Tag Zero", "Conversation"),
        ),
        taxonomy,
        TaxonomyCapabilities(True, True, True, True),
    )
    return job, review, FakeIngestion()


@pytest.mark.asyncio
async def test_ai_review_layout_title_date_and_metadata_interactions(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    settings = settings_factory(data_dir=tmp_path)
    view = AISuggestionsView(job, review, cast(Any, ingestion), settings)

    assert view.is_dirty
    assert view.has_editable_fields
    assert view.title_content() == ("**Title**\nChat Conversation with Mary *(pending)*")
    assert "Each menu identifies its field" in view.metadata_content()
    assert "Pending changes" in view.actions_content()
    assert view.current_title == "Chat Conversation with Mary"
    assert view.date_placeholder() == "Date: 2026-07-28 (Paperless suggestion)"
    assert view.metadata_placeholder(TaxonomyKind.CORRESPONDENT) == (
        "Correspondent - Pending - Mary"
    )
    assert view.metadata_placeholder(TaxonomyKind.TAG) == "Tags: 1 selected"

    children = list(view.children)
    assert isinstance(children[0], _DateSelect)
    assert [item.kind for item in children if isinstance(item, _MetadataSelect)] == [
        TaxonomyKind.CORRESPONDENT,
        TaxonomyKind.DOCUMENT_TYPE,
        TaxonomyKind.STORAGE_PATH,
        TaxonomyKind.TAG,
    ]
    assert all(
        option.label.startswith("Correspondent ·")
        for option in view.options_for(TaxonomyKind.CORRESPONDENT)
    )

    thread = FakeThread(parent_id=102)
    await view.send(thread)
    assert [message.content.splitlines()[0] for message in thread.sent] == [
        "**Title**",
        "**Editable Metadata**",
        "**Pending changes**",
    ]
    assert isinstance(thread.sent[0].send_kwargs["view"], _TitleEditView)
    assert thread.sent[1].send_kwargs["view"] is view
    assert isinstance(thread.sent[2].send_kwargs["view"], _ReviewActionsView)

    unauthorized = AsyncMock(user=SimpleNamespace(id=999))
    assert not await view.interaction_check(unauthorized)
    assert "Only the user who uploaded" in unauthorized.response.send_message.call_args.args[0]
    assert await view.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))

    title_view = thread.sent[0].send_kwargs["view"]
    assert await title_view.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    title_interaction = AsyncMock(user=SimpleNamespace(id=201))
    await title_view.edit_title.callback(title_interaction)
    title_modal = title_interaction.response.send_modal.call_args.args[0]
    assert isinstance(title_modal, AISuggestionsTitleModal)
    title_modal.title_input._value = "  User Edited Title  "
    await title_modal.on_submit(AsyncMock(user=SimpleNamespace(id=201)))
    assert view.current_title == "User Edited Title"
    assert view.title_message is not None
    assert "User Edited Title" in view.title_message.content

    title_modal.title_input._value = ""
    await title_modal.on_submit(AsyncMock(user=SimpleNamespace(id=201)))
    assert view.current_title == "Current"
    denied_title = AsyncMock(user=SimpleNamespace(id=999))
    await title_modal.on_submit(denied_title)
    assert "Only the uploader" in denied_title.response.send_message.call_args.args[0]

    date_select = next(item for item in view.children if isinstance(item, _DateSelect))
    assert date_select.options[0].label.startswith("Date · Keep current")
    date_select._values = ["date:2026-07-28"]
    await date_select.callback(AsyncMock())
    assert view.selection.created == date(2026, 7, 28)
    date_select = next(item for item in view.children if isinstance(item, _DateSelect))
    date_select._values = ["keep"]
    await date_select.callback(AsyncMock())
    assert view.selection.created is None
    date_select = next(item for item in view.children if isinstance(item, _DateSelect))
    date_select._values = ["custom"]
    custom_interaction = AsyncMock()
    await date_select.callback(custom_interaction)
    date_modal = custom_interaction.response.send_modal.call_args.args[0]
    assert isinstance(date_modal, AISuggestionsDateModal)
    date_modal.date_input._value = "bad"
    invalid_date = AsyncMock(user=SimpleNamespace(id=201))
    await date_modal.on_submit(invalid_date)
    assert "valid date" in invalid_date.response.send_message.call_args.args[0]
    denied_date = AsyncMock(user=SimpleNamespace(id=999))
    await date_modal.on_submit(denied_date)
    assert "Only the uploader" in denied_date.response.send_message.call_args.args[0]
    date_modal.date_input._value = "2026-07-30"
    await date_modal.on_submit(AsyncMock(user=SimpleNamespace(id=201)))
    assert view.selection.created == date(2026, 7, 30)
    assert view.date_placeholder() == "Date: 2026-07-30 (custom)"

    correspondent = next(
        item
        for item in view.children
        if isinstance(item, _MetadataSelect) and item.kind is TaxonomyKind.CORRESPONDENT
    )
    correspondent._values = ["new:0"]
    await correspondent.callback(AsyncMock())
    assert view.selection.correspondent_id is None
    assert view.selection.new_correspondents == ("Mary Smyth",)
    assert view.metadata_placeholder(TaxonomyKind.CORRESPONDENT).endswith("(new)")

    reset_interaction = AsyncMock()
    await view.reset(reset_interaction)
    assert view.selection == view.initial_selection
    assert "reset to Paperless" in reset_interaction.followup.send.call_args.args[0]

    actions_view = thread.sent[2].send_kwargs["view"]
    assert await actions_view.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    request_apply_mock = AsyncMock()
    reset_mock = AsyncMock()
    view.request_apply = request_apply_mock
    view.reset = reset_mock
    await actions_view.apply_changes.callback(AsyncMock())
    await actions_view.reset_changes.callback(AsyncMock())
    request_apply_mock.assert_awaited_once()
    reset_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_suggestion_combined_selectors_and_pagination(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, base_review, ingestion = _ai_review_fixture(tmp_path)
    suggestions = AISuggestions(
        correspondent_ids=(1,),
        document_type_ids=(3,),
        storage_path_ids=(4,),
        tag_ids=(2,),
        suggested_correspondents=("Mary Smyth",),
        suggested_document_types=("Text Message",),
        suggested_storage_paths=("Messages/Mary",),
        suggested_tags=tuple(f"New Tag {index}" for index in range(30)),
    )
    review = SuggestionReview(
        base_review.document,
        suggestions,
        base_review.taxonomy,
        TaxonomyCapabilities(True, True, True, True),
    )
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    unchanged = await view._resolve_new_selection()
    assert unchanged == view.selection
    selects = {item.kind: item for item in view.children if isinstance(item, _MetadataSelect)}

    correspondent_options = view.options_for(TaxonomyKind.CORRESPONDENT)
    assert [option.value for option in correspondent_options] == [
        "keep",
        "id:1",
        "id:9",
        "new:0",
    ]
    assert correspondent_options[1].default
    assert not correspondent_options[-1].default
    assert view._selected_option_values(TaxonomyKind.CORRESPONDENT) == ("id:1",)
    assert selects[TaxonomyKind.TAG].options[-1].value == "more"
    assert "33 choices" in selects[TaxonomyKind.TAG].options[-1].label

    source_message = AsyncMock(spec=discord.Message)
    more_interaction = AsyncMock(
        user=SimpleNamespace(id=201),
        message=source_message,
    )
    selects[TaxonomyKind.TAG]._values = ["more"]
    await selects[TaxonomyKind.TAG].callback(more_interaction)
    overflow = more_interaction.response.send_message.call_args.kwargs["view"]
    assert isinstance(overflow, _MetadataOverflowView)
    assert overflow.page_count == 2
    assert overflow.previous_button.disabled
    assert not overflow.next_button.disabled

    other_user = AsyncMock(user=SimpleNamespace(id=999))
    assert not await overflow.interaction_check(other_user)
    owner = AsyncMock(user=SimpleNamespace(id=201))
    assert await overflow.interaction_check(owner)

    next_interaction = AsyncMock()
    await overflow.next_button.callback(next_interaction)
    assert overflow.page == 1
    assert overflow.next_button.disabled
    page_select = next(item for item in overflow.children if isinstance(item, _MetadataPageSelect))
    page_select._values = ["new:29"]
    page_interaction = AsyncMock()
    await page_select.callback(page_interaction)
    assert view.selection.tag_ids == (2,)
    assert view.selection.new_tags == ("New Tag 29",)

    overflow_without_source = _MetadataOverflowView(
        view,
        TaxonomyKind.TAG,
        page=1,
    )
    page_without_source = next(
        item for item in overflow_without_source.children if isinstance(item, _MetadataPageSelect)
    )
    page_without_source._values = ["new:29"]
    await page_without_source.callback(AsyncMock())

    previous_interaction = AsyncMock()
    await overflow.previous_button.callback(previous_interaction)
    assert overflow.page == 0
    done_interaction = AsyncMock()
    await overflow.done_button.callback(done_interaction)
    assert done_interaction.response.edit_message.call_args.kwargs["view"] is None

    scalar_select = selects[TaxonomyKind.CORRESPONDENT]
    scalar_select._values = ["new:0"]
    scalar_interaction = AsyncMock(message=source_message)
    await scalar_select.callback(scalar_interaction)
    assert view.selection.correspondent_id is None
    assert view.selection.new_correspondents == ("Mary Smyth",)

    no_message_select = next(
        item
        for item in view.children
        if isinstance(item, _MetadataSelect) and item.kind is TaxonomyKind.CORRESPONDENT
    )
    no_message_select._values = ["keep"]
    no_message_interaction = AsyncMock(message=None)
    await no_message_select.callback(no_message_interaction)
    no_message_interaction.response.defer.assert_awaited_once()

    view.update_selection(TaxonomyKind.DOCUMENT_TYPE, ("new:0",))
    view.update_selection(TaxonomyKind.STORAGE_PATH, ("new:0",))
    assert view.selection.new_document_types == ("Text Message",)
    assert view.selection.new_storage_paths == ("Messages/Mary",)
    assert view._selected_option_values(TaxonomyKind.DOCUMENT_TYPE) == ("new:0",)
    assert view._selected_option_values(TaxonomyKind.TAG) == ("id:2", "new:29")
    view.update_selection(TaxonomyKind.TAG, ("id:2", "id:8", "new:29"))
    assert view.metadata_placeholder(TaxonomyKind.TAG) == "Tags: 3 selected"

    assert _bounded_lines(()) == "None"
    assert _bounded_lines(("x" * 1100,)).endswith("…")
    close = _close_existing_items(
        ("Mary Smit", "Mary Smith"),
        review.taxonomy.correspondents,
        (),
    )
    assert close == ((TaxonomyItem(9, "Mary Smith"), "Mary Smith"),)
    assert _close_existing_items(
        ("Mary Smith", "Mary Smit"),
        review.taxonomy.correspondents,
        (),
    ) == ((TaxonomyItem(9, "Mary Smith"), "Mary Smith"),)


@pytest.mark.asyncio
async def test_ai_new_taxonomy_confirmation_is_configurable(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, base_review, ingestion = _ai_review_fixture(tmp_path)
    review = replace(
        base_review,
        suggestions=AISuggestions(
            suggested_correspondents=("Abby",),
            suggested_document_types=("Text Message",),
            suggested_storage_paths=("Messages/Mary",),
            suggested_tags=("new-topic",),
        ),
        taxonomy=Taxonomy((), (), (), ()),
    )
    ingestion.apply_suggestions = AsyncMock(  # type: ignore[method-assign]
        return_value=SuggestionApplyResult(review.document, True)
    )
    ingestion.resolve_or_create_taxonomy = AsyncMock(  # type: ignore[method-assign]
        side_effect=(
            TaxonomyItem(101, "new-topic"),
            TaxonomyItem(102, "Abby"),
            TaxonomyItem(103, "Text Message"),
            TaxonomyItem(104, "Messages/Mary"),
        )
    )
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    view.update_selection(TaxonomyKind.TAG, ("new:0",))
    view.update_selection(TaxonomyKind.CORRESPONDENT, ("new:0",))
    view.update_selection(TaxonomyKind.DOCUMENT_TYPE, ("new:0",))
    view.update_selection(TaxonomyKind.STORAGE_PATH, ("new:0",))

    approve = AsyncMock(user=SimpleNamespace(id=201))
    await view.request_apply(approve)
    confirmation = approve.response.send_message.call_args.kwargs["view"]
    assert isinstance(confirmation, _ConfirmTaxonomyCreationView)
    assert "New names" not in approve.response.send_message.call_args.args[0]

    denied = AsyncMock(user=SimpleNamespace(id=999))
    await confirmation.confirm.callback(denied)
    denied.response.send_message.assert_awaited_once()

    back = AsyncMock()
    await confirmation.back.callback(back)
    assert "No taxonomy" in back.response.edit_message.call_args.kwargs["content"]

    confirmed = AsyncMock(user=SimpleNamespace(id=201))
    await confirmation.confirm.callback(confirmed)
    ingestion.apply_suggestions.assert_awaited_once()
    updates = ingestion.apply_suggestions.call_args.args[1]
    assert updates.tag_ids == (101,)
    assert updates.correspondent_id == 102
    assert updates.document_type_id == 103
    assert updates.storage_path_id == 104

    silent_ingestion = FakeIngestion()
    silent_ingestion.apply_suggestions = AsyncMock(  # type: ignore[method-assign]
        return_value=SuggestionApplyResult(review.document, True)
    )
    silent_ingestion.resolve_or_create_taxonomy = AsyncMock(  # type: ignore[method-assign]
        return_value=TaxonomyItem(200, "new-topic")
    )
    silent = AISuggestionsView(
        job,
        review,
        cast(Any, silent_ingestion),
        settings_factory(
            data_dir=tmp_path,
            require_new_metadata_confirmation=False,
        ),
    )
    silent.update_selection(TaxonomyKind.TAG, ("new:0",))
    silent_apply = AsyncMock(user=SimpleNamespace(id=201))
    await silent.request_apply(silent_apply)
    silent_apply.response.defer.assert_awaited_once_with(ephemeral=True)
    silent_ingestion.resolve_or_create_taxonomy.assert_awaited_once()
    silent_ingestion.apply_suggestions.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_review_apply_errors_reload_and_disabled_fields(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    disabled_settings = settings_factory(
        data_dir=tmp_path,
        allow_edit_title=False,
        allow_edit_date=False,
        allow_edit_correspondent=False,
        allow_edit_document_type=False,
        allow_edit_storage_path=False,
        allow_edit_tags=False,
    )
    disabled = AISuggestionsView(job, review, cast(Any, ingestion), disabled_settings)
    assert not disabled.has_editable_fields
    assert not disabled.is_dirty
    assert disabled.current_title == "Current"
    assert not disabled.children
    disabled_thread = FakeThread(parent_id=102)
    await disabled.send(disabled_thread)
    assert not disabled_thread.sent
    disabled_modal = AISuggestionsTagModal(disabled)
    disabled_modal.tag_input._value = "blocked"
    disabled_tag = AsyncMock(user=SimpleNamespace(id=201))
    await disabled_modal.on_submit(disabled_tag)
    assert "disabled" in disabled_tag.response.send_message.call_args.args[0]

    title_only = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(
            data_dir=tmp_path,
            allow_edit_date=False,
            allow_edit_correspondent=False,
            allow_edit_document_type=False,
            allow_edit_storage_path=False,
            allow_edit_tags=False,
        ),
    )
    assert "Add Tag" not in {
        cast(discord.ui.Button[Any], child).label
        for child in _ReviewActionsView(title_only).children
    }

    partial = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(
            data_dir=tmp_path,
            allow_edit_title=False,
            allow_edit_date=False,
            allow_edit_correspondent=False,
            allow_edit_document_type=False,
            allow_edit_storage_path=False,
        ),
    )
    assert partial.selection.title is None
    assert partial.selection.created is None
    assert partial.selection.correspondent_id is None
    assert partial.selection.document_type_id is None
    assert partial.selection.storage_path_id is None
    assert partial.selection.tag_ids == (2,)
    assert [item.kind for item in partial.children if isinstance(item, _MetadataSelect)] == [
        TaxonomyKind.TAG
    ]

    for error, expected in (
        (StaleSuggestionError("stale"), "changed after"),
        (UnlinkedUserError("unlinked"), "no longer linked"),
        (PaperlessUnavailableError("unavailable"), "could not resolve"),
        (Exception("secret detail"), "could not be applied"),
    ):
        ingestion.apply_suggestions = AsyncMock(side_effect=error)  # type: ignore[method-assign]
        failed = AISuggestionsView(
            job,
            review,
            cast(Any, ingestion),
            settings_factory(data_dir=tmp_path),
        )
        interaction = AsyncMock()
        await failed.apply(interaction, confirm_create=False)
        assert expected in interaction.followup.send.call_args.args[0]
        assert "secret detail" not in interaction.followup.send.call_args.args[0]

    ingestion.apply_suggestions = AsyncMock(  # type: ignore[method-assign]
        return_value=SuggestionApplyResult(review.document, True)
    )
    applied = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    interaction = AsyncMock()
    await applied.apply(interaction, confirm_create=False)
    assert applied._applied
    assert not applied.is_dirty
    assert "Changes applied" in applied.actions_content()
    assert "confirmed" in interaction.followup.send.call_args.args[0]
    already = AsyncMock()
    await applied.apply(already, confirm_create=False)
    assert "already applied" in already.followup.send.call_args.args[0]
    actions = _ReviewActionsView(applied)
    assert actions.apply_changes.disabled
    assert actions.reset_changes.disabled
    title_actions = _TitleEditView(applied)
    assert title_actions.edit_title.disabled

    for error, expected in (
        (StaleSuggestionError("stale"), "changed while"),
        (UnlinkedUserError("unlinked"), "no longer linked"),
        (PaperlessUnavailableError("unavailable"), "unavailable"),
    ):
        ingestion.get_suggestion_review = AsyncMock(side_effect=error)  # type: ignore[method-assign]
        assert expected in (await applied.reload() or "")
    ingestion.get_suggestion_review = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await applied.reload() is None
    ingestion.get_suggestion_review = AsyncMock(return_value=review)  # type: ignore[method-assign]
    assert await applied.reload() is None
    assert not applied._applied
    assert applied.is_dirty
    applied.saved_selection = applied.selection
    assert applied.actions_content() == "**No pending changes**"


@pytest.mark.asyncio
async def test_individual_save_reports_metadata_success_tag_finalization_failure(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    ingestion.apply_suggestions = AsyncMock(  # type: ignore[method-assign]
        return_value=SuggestionApplyResult(
            review.document,
            False,
            "Paperless saved the metadata, but review-tag finalization needs reconciliation.",
        )
    )
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    interaction = AsyncMock()

    finalized = await view.apply(interaction, confirm_create=False)

    assert not finalized
    assert view._needs_finalization
    assert view._last_apply_metadata_saved
    assert "saved the metadata" in interaction.followup.send.call_args.args[0]
    assert "reconciliation" in view.actions_content()
    assert ingestion.finalization_notifications == []


@pytest.mark.asyncio
async def test_individual_save_opens_cleanup_gate_after_response(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    events: list[str] = []
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    interaction = AsyncMock()
    interaction.followup.send.side_effect = lambda *args, **kwargs: events.append("response")

    async def notified(saved_job: IngestionJob) -> None:
        assert saved_job.id == job.id
        assert events == ["response"]
        events.append("cleanup-ready")

    ingestion.mark_review_finalization_notified = notified  # type: ignore[assignment]

    assert await view.apply(interaction, confirm_create=False)
    assert events == ["response", "cleanup-ready"]


@pytest.mark.asyncio
async def test_explicit_no_difference_save_still_finalizes_review(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    view.saved_selection = view.selection
    interaction = AsyncMock()

    assert not view.is_dirty
    await view.request_apply(interaction)

    assert ingestion.finalization_notifications == [job.id]
    assert "finalized the review tags" in interaction.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_save_all_reports_tag_failure_without_releasing_cleanup(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    ingestion.apply_suggestions = AsyncMock(  # type: ignore[method-assign]
        return_value=SuggestionApplyResult(
            review.document,
            False,
            "Paperless saved metadata but finalization failed.",
        )
    )
    session = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    controller = _ReviewThreadController(201, 900)
    controller.add(session)
    batch = _UploadBatchController(201, 900)
    batch.add(controller)
    interaction = AsyncMock()

    await batch.save_all(interaction)

    assert "1 metadata saved" in interaction.followup.send.call_args.args[0]
    assert ingestion.finalization_notifications == []
    assert session in batch.saveable_sessions


@pytest.mark.asyncio
async def test_save_all_releases_cleanup_only_after_summary_response(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    session = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    controller = _ReviewThreadController(201, 900)
    controller.add(session)
    batch = _UploadBatchController(201, 900)
    batch.add(controller)
    events: list[str] = []
    interaction = AsyncMock()
    interaction.followup.send.side_effect = lambda *args, **kwargs: events.append("summary")

    async def notified(saved_job: IngestionJob) -> None:
        assert saved_job.id == job.id
        assert events == ["summary"]
        events.append("cleanup-ready")

    ingestion.mark_review_finalization_notified = notified  # type: ignore[assignment]

    await batch.save_all(interaction)
    assert events == ["summary", "cleanup-ready"]


@pytest.mark.asyncio
async def test_apply_missing_document_result_is_generic_and_safe(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    ingestion.apply_suggestions = AsyncMock(return_value=None)  # type: ignore[method-assign]
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    interaction = AsyncMock()

    assert not await view.apply(interaction, confirm_create=False)
    assert "could not resolve or apply" in interaction.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_silent_batch_apply_failure_does_not_send_per_item_response(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    ingestion.apply_suggestions = AsyncMock(  # type: ignore[method-assign]
        side_effect=StaleSuggestionError("stale")
    )
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    interaction = AsyncMock()

    assert not await view.apply(interaction, confirm_create=False, respond=False)
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_silent_already_applied_review_does_not_repeat_response(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    view._applied = True
    interaction = AsyncMock()

    assert await view.apply(interaction, confirm_create=False, respond=False)
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_reset_cancel_close_and_recovery_do_not_apply_metadata(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    await view.reload()
    await view.reset(AsyncMock())
    confirmation = _ConfirmTaxonomyCreationView(view)
    await confirmation.back.callback(AsyncMock())
    controller = _ReviewThreadController(201, 900)
    controller.add(view)
    await controller.finish(AsyncMock(channel=FakeThread(102)))

    assert ingestion.applied_updates is None
    assert ingestion.finalization_notifications == []


@pytest.mark.asyncio
async def test_custom_tag_modal_and_rich_parent_synchronization(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    parent = FakeMessage(channel=FakeChannel(102), content="initial")
    view = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
        ordinal=2,
        total_items=3,
        parent_message=cast(discord.Message, parent),
        public_url="https://paperless.example.test/documents/7/details",
    )
    denied = AsyncMock(user=SimpleNamespace(id=999))
    modal = AISuggestionsTagModal(view)
    modal.tag_input._value = "Existing Tag"
    await modal.on_submit(denied)
    assert "Only the uploader" in denied.response.send_message.call_args.args[0]

    blank = AsyncMock(user=SimpleNamespace(id=201))
    modal.tag_input._value = "   "
    await modal.on_submit(blank)
    assert "non-empty" in blank.response.send_message.call_args.args[0]

    duplicate_taxonomy = replace(
        review.taxonomy,
        tags=(
            *review.taxonomy.tags,
            TaxonomyItem(99, "existing tag"),
        ),
    )
    view.review = replace(view.review, taxonomy=duplicate_taxonomy)
    ambiguous = AsyncMock(user=SimpleNamespace(id=201))
    modal.tag_input._value = "Existing Tag"
    await modal.on_submit(ambiguous)
    assert "More than one" in ambiguous.response.send_message.call_args.args[0]

    view.review = review
    existing = AsyncMock(user=SimpleNamespace(id=201))
    modal.tag_input._value = " existing tag "
    await modal.on_submit(existing)
    assert view.selection.tag_ids == (2,)
    assert "existing tag" in existing.followup.send.call_args.args[0].casefold()
    assert parent.edits

    created = AsyncMock(user=SimpleNamespace(id=201))
    modal.tag_input._value = " Cafe\u0301 "
    await modal.on_submit(created)
    assert view.selection.new_tags == ("Café",)
    modal.tag_input._value = "CAFÉ"
    await modal.on_submit(AsyncMock(user=SimpleNamespace(id=201)))
    assert view.selection.new_tags == ("Café",)
    custom_option = next(
        option
        for option in view.options_for(TaxonomyKind.TAG)
        if option.value.startswith("custom:")
    )
    view.update_selection(
        TaxonomyKind.TAG,
        ("id:2", custom_option.value),
    )
    assert view.selection.new_tags == ("Café",)
    assert custom_option.value in view._selected_option_values(TaxonomyKind.TAG)

    view.selection = replace(
        view.selection,
        new_correspondents=("A" * 220,),
        new_document_types=("New Type",),
        new_storage_paths=("New/Path",),
    )
    content = view.parent_content()
    assert "Document 2/3" in content
    assert "Current — Chat Log" in content
    assert "Pending — New Type (new)" in content
    assert "**Storage Path:**" in content
    assert "**Tags:**" in content
    assert len(content) <= 1900
    assert view._taxonomy_value(None, TaxonomyKind.TAG) == "None"
    view._applied = True
    assert "**Status:** Saved" in view.parent_content()

    add_tag_button = next(
        child
        for child in _ReviewActionsView(view).children
        if cast(discord.ui.Button[Any], child).label == "Add Tag"
    )
    open_modal = AsyncMock(user=SimpleNamespace(id=201))
    await cast(discord.ui.Button[Any], add_tag_button).callback(open_modal)
    assert isinstance(open_modal.response.send_modal.call_args.args[0], AISuggestionsTagModal)


@pytest.mark.asyncio
async def test_ai_review_thread_controls_are_persistent_and_guard_dirty_state(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    job, review, ingestion = _ai_review_fixture(tmp_path)
    session = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings_factory(data_dir=tmp_path),
    )
    controller = _ReviewThreadController(201, 900)
    controller.add(session)
    assert controller.is_dirty

    denied = AsyncMock(user=SimpleNamespace(id=999))
    assert not await controller.interaction_check(denied)
    assert "Only the uploader" in denied.response.send_message.call_args.args[0]
    assert await controller.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))

    controls = controller.build_view("https://paperless.example.test/documents/7/details")
    assert isinstance(controls, _ReviewThreadControlsView)
    assert await controls.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    assert [cast(discord.ui.Button[Any], child).label for child in controls.children] == [
        "Open Paperless",
        "Refresh",
        "Finish & Close",
    ]
    no_link_controls = controller.build_view(None)
    assert [cast(discord.ui.Button[Any], child).label for child in no_link_controls.children] == [
        "Refresh",
        "Finish & Close",
    ]

    dirty_refresh = AsyncMock(user=SimpleNamespace(id=201))
    await controller.request_refresh(dirty_refresh)
    refresh_confirmation = dirty_refresh.response.send_message.call_args.kwargs["view"]
    assert isinstance(refresh_confirmation, _ConfirmRefreshView)
    assert await refresh_confirmation.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    back_refresh = AsyncMock(user=SimpleNamespace(id=201))
    await refresh_confirmation.back.callback(back_refresh)
    assert "canceled" in back_refresh.response.edit_message.call_args.kwargs["content"]

    session.reload = AsyncMock(return_value=None)  # type: ignore[method-assign]
    confirmed_refresh = AsyncMock(user=SimpleNamespace(id=201))
    await refresh_confirmation.confirm.callback(confirmed_refresh)
    session.reload.assert_awaited_once()
    assert "refreshed" in confirmed_refresh.followup.send.call_args.args[0]

    session.reload = AsyncMock(return_value="Synthetic refresh error")  # type: ignore[method-assign]
    session.saved_selection = session.selection
    direct_refresh = AsyncMock(user=SimpleNamespace(id=201))
    await controller.request_refresh(direct_refresh)
    assert "Synthetic refresh error" in direct_refresh.followup.send.call_args.args[0]

    refresh_button = next(
        child for child in controls.children if isinstance(child, _RefreshReviewButton)
    )
    session.reload = AsyncMock(return_value=None)  # type: ignore[method-assign]
    await refresh_button.callback(AsyncMock(user=SimpleNamespace(id=201)))

    session.selection = replace(session.selection, title="Unsaved")
    dirty_close = AsyncMock(user=SimpleNamespace(id=201))
    await controller.request_finish(dirty_close)
    close_confirmation = dirty_close.response.send_message.call_args.kwargs["view"]
    assert isinstance(close_confirmation, _ConfirmCloseThreadView)
    assert await close_confirmation.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    back_close = AsyncMock(user=SimpleNamespace(id=201))
    await close_confirmation.back.callback(back_close)
    assert "left open" in back_close.response.edit_message.call_args.kwargs["content"]

    channel = SimpleNamespace(delete=AsyncMock())
    confirmed_close = AsyncMock(user=SimpleNamespace(id=201), channel=channel)
    await close_confirmation.confirm.callback(confirmed_close)
    channel.delete.assert_awaited_once_with(reason="Uploader finished Paperless document review")

    session.saved_selection = session.selection
    direct_channel = SimpleNamespace(delete=AsyncMock())
    direct_close = AsyncMock(user=SimpleNamespace(id=201), channel=direct_channel)
    await controller.request_finish(direct_close)
    direct_channel.delete.assert_awaited_once()

    finish_button = next(
        child for child in controls.children if isinstance(child, _FinishReviewButton)
    )
    button_channel = SimpleNamespace(delete=AsyncMock())
    await finish_button.callback(AsyncMock(user=SimpleNamespace(id=201), channel=button_channel))
    button_channel.delete.assert_awaited_once()

    invalid_channel = AsyncMock()
    invalid_channel.channel = SimpleNamespace()
    await controller.finish(invalid_channel)
    assert "only available" in invalid_channel.followup.send.call_args.args[0]

    delete_error = discord.HTTPException(
        cast(Any, SimpleNamespace(status=403, reason="Forbidden")),
        "synthetic",
    )
    failed_channel = SimpleNamespace(delete=AsyncMock(side_effect=delete_error))
    failed_delete = AsyncMock(channel=failed_channel)
    await controller.finish(failed_delete)
    assert "Manage Threads" in failed_delete.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_bound_review_batch_controls_and_failure_dismissal(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    job, review, ingestion = _ai_review_fixture(tmp_path)
    await ingestion.create_upload_batch(
        UploadBatch(1, 102, 50, 102, 201, 1),
        (
            UploadItem(
                1,
                2,
                1,
                "synthetic.pdf",
                state=UploadItemState.SUCCEEDED,
                job_id=job.id,
                document_id=DocumentId(7),
            ),
        ),
    )
    parent = FakeMessage(channel=FakeChannel(102), content="rich parent")
    thread = FakeThread(102)
    session = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings,
        parent_message=cast(discord.Message, parent),
    )
    session.saved_selection = session.selection
    cleanup = AsyncMock()
    bound = _ReviewThreadController(
        201,
        900,
        ingestion=cast(Any, ingestion),
        source_message_id=1,
        attachment_id=2,
        parent_message=cast(discord.Message, parent),
        thread=thread,
        cleanup_callback=cleanup,
    )
    bound.add(session)
    finish_interaction = AsyncMock(user=SimpleNamespace(id=201), channel=thread)
    await bound.request_finish(finish_interaction)
    assert bound.closed
    cleanup.assert_awaited_once_with(
        1,
        2,
        (
            DiscordMessageTarget(102, 50),
            DiscordMessageTarget(102, 1),
        ),
    )
    await bound.finish(finish_interaction)
    assert "already closed" in finish_interaction.followup.send.call_args.args[0]

    no_edit_bound = _ReviewThreadController(
        201,
        900,
        ingestion=cast(Any, ingestion),
        source_message_id=1,
        attachment_id=2,
        thread=cast(discord.Thread, SimpleNamespace()),
    )
    await no_edit_bound.finish(AsyncMock(channel=SimpleNamespace()))
    assert no_edit_bound.closed

    first = AISuggestionsView(
        replace(job, discord_attachment_id=3, original_filename="second.pdf"),
        review,
        cast(Any, ingestion),
        settings,
        ordinal=2,
        total_items=2,
    )
    second = AISuggestionsView(
        job,
        review,
        cast(Any, ingestion),
        settings,
        ordinal=1,
        total_items=2,
    )
    second.selection = replace(second.selection, new_tags=("new-topic",))
    first_controller = _ReviewThreadController(201, 900)
    first_controller.add(first)
    second_controller = _ReviewThreadController(201, 900)
    second_controller.add(second)
    batch = _UploadBatchController(201, 900)
    batch.add(first_controller)
    batch.add(second_controller)
    assert tuple(item.ordinal for item in batch.sessions) == (1, 2)

    denied = AsyncMock(user=SimpleNamespace(id=999))
    assert not await batch.interaction_check(denied)
    assert await batch.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    save_prompt = AsyncMock(user=SimpleNamespace(id=201))
    await batch.request_save_all(save_prompt)
    assert "Potential new" in save_prompt.response.send_message.call_args.args[0]
    assert isinstance(
        save_prompt.response.send_message.call_args.kwargs["view"],
        _ConfirmSaveAllView,
    )

    async def save_success(
        interaction: Any,
        *,
        confirm_create: bool,
        respond: bool,
    ) -> bool:
        del interaction
        assert confirm_create
        assert not respond
        second._applied = True
        return True

    async def save_failure(
        interaction: Any,
        *,
        confirm_create: bool,
        respond: bool,
    ) -> bool:
        del interaction
        assert not confirm_create
        assert not respond
        return False

    second.apply = save_success  # type: ignore[assignment]
    first.apply = save_failure  # type: ignore[assignment]
    save_interaction = AsyncMock(user=SimpleNamespace(id=201))
    await batch.save_all(save_interaction)
    assert "1 saved" in save_interaction.followup.send.call_args.args[0]
    assert (
        "1 failed before metadata confirmation"
        in (save_interaction.followup.send.call_args.args[0])
    )

    first.saved_selection = first.selection
    second.saved_selection = second.selection
    no_changes = AsyncMock(user=SimpleNamespace(id=201))
    await batch.request_save_all(no_changes)
    assert "no pending" in no_changes.response.send_message.call_args.args[0]

    first.reload = AsyncMock(return_value="refresh failed")  # type: ignore[method-assign]
    second.reload = AsyncMock(return_value=None)  # type: ignore[method-assign]
    refreshed = AsyncMock(user=SimpleNamespace(id=201))
    await batch.refresh_all(refreshed)
    assert "refresh failed" in refreshed.followup.send.call_args.args[0]
    first.reload = AsyncMock(return_value=None)  # type: ignore[method-assign]
    await batch.refresh_all(AsyncMock(user=SimpleNamespace(id=201)))

    clean_close_prompt = AsyncMock(user=SimpleNamespace(id=201))
    await batch.request_close_all(clean_close_prompt)
    assert "will be discarded" not in clean_close_prompt.response.send_message.call_args.args[0]
    first.selection = replace(first.selection, title="Unsaved")
    save_without_new = AsyncMock(user=SimpleNamespace(id=201))
    await batch.request_save_all(save_without_new)
    assert "Potential new" not in save_without_new.response.send_message.call_args.args[0]
    close_prompt = AsyncMock(user=SimpleNamespace(id=201))
    await batch.request_close_all(close_prompt)
    assert "will be discarded" in close_prompt.response.send_message.call_args.args[0]
    assert isinstance(
        close_prompt.response.send_message.call_args.kwargs["view"],
        _ConfirmCloseAllView,
    )

    async def close_first(interaction: Any) -> None:
        first_controller.closed = True

    async def close_second(interaction: Any) -> None:
        second_controller.closed = True

    first_controller.finish = close_first  # type: ignore[method-assign]
    second_controller.finish = close_second  # type: ignore[method-assign]
    close_interaction = AsyncMock(user=SimpleNamespace(id=201))
    await batch.close_all(close_interaction)
    assert "2 successful" in close_interaction.followup.send.call_args.args[0]
    await batch.close_all(AsyncMock(user=SimpleNamespace(id=201)))
    no_open = AsyncMock(user=SimpleNamespace(id=201))
    await batch.request_close_all(no_open)
    assert "no successful" in no_open.response.send_message.call_args.args[0]

    controls = batch.build_view()
    assert isinstance(controls, _UploadBatchControlsView)
    assert await controls.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    batch.refresh_all = AsyncMock()  # type: ignore[method-assign]
    batch.request_save_all = AsyncMock()  # type: ignore[method-assign]
    batch.request_close_all = AsyncMock()  # type: ignore[method-assign]
    for child in controls.children:
        await cast(discord.ui.Button[Any], child).callback(AsyncMock(user=SimpleNamespace(id=201)))
    batch.refresh_all.assert_awaited_once()
    batch.request_save_all.assert_awaited_once()
    batch.request_close_all.assert_awaited_once()

    confirm_save = _ConfirmSaveAllView(batch)
    confirm_close = _ConfirmCloseAllView(batch)
    assert await confirm_save.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    assert await confirm_close.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    batch.save_all = AsyncMock()  # type: ignore[method-assign]
    batch.close_all = AsyncMock()  # type: ignore[method-assign]
    await confirm_save.confirm.callback(AsyncMock(user=SimpleNamespace(id=201)))
    await confirm_close.confirm.callback(AsyncMock(user=SimpleNamespace(id=201)))
    batch.save_all.assert_awaited_once()
    batch.close_all.assert_awaited_once()

    failure_ingestion = FakeIngestion()
    await failure_ingestion.create_upload_batch(
        UploadBatch(9, 102, 59, 102, 201, 1),
        (UploadItem(9, 90, 1, "failed.pdf", state=UploadItemState.FAILED),),
    )
    failure_parent = FakeMessage(channel=FakeChannel(102), content="failed")
    failure_thread = FakeThread(102)
    failure_cleanup = AsyncMock()
    failure = _FailedUploadController(
        principal_id=201,
        ingestion=cast(Any, failure_ingestion),
        source_message_id=9,
        attachment_id=90,
        parent_message=cast(discord.Message, failure_parent),
        thread=failure_thread,
        cleanup_callback=failure_cleanup,
    )
    denied_failure = AsyncMock(user=SimpleNamespace(id=999))
    assert not await failure.interaction_check(denied_failure)
    assert await failure.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    dismiss_prompt = AsyncMock(user=SimpleNamespace(id=201))
    await failure.request_dismiss(dismiss_prompt)
    confirmation = dismiss_prompt.response.send_message.call_args.kwargs["view"]
    assert isinstance(confirmation, _ConfirmDismissFailedView)
    assert await confirmation.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    confirmed = AsyncMock(user=SimpleNamespace(id=201))
    await confirmation.confirm.callback(confirmed)
    assert failure.dismissed
    failure_cleanup.assert_awaited_once_with(
        9,
        90,
        (
            DiscordMessageTarget(102, 59),
            DiscordMessageTarget(102, 9),
        ),
    )
    await failure.dismiss(confirmed)
    assert "already dismissed" in confirmed.followup.send.call_args.args[0]

    failure_view = _FailedUploadView(failure, 900)
    assert await failure_view.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    failure.request_dismiss = AsyncMock()  # type: ignore[method-assign]
    await failure_view.dismiss.callback(AsyncMock(user=SimpleNamespace(id=201)))
    failure.request_dismiss.assert_awaited_once()

    unresolved_ingestion = FakeIngestion()
    await unresolved_ingestion.create_upload_batch(
        UploadBatch(19, 102, 69, 102, 201, 2),
        (
            UploadItem(19, 190, 1, "failed.pdf", state=UploadItemState.FAILED),
            UploadItem(19, 191, 2, "pending.pdf", state=UploadItemState.PENDING),
        ),
    )
    no_target_failure = _FailedUploadController(
        principal_id=201,
        ingestion=cast(Any, unresolved_ingestion),
        source_message_id=19,
        attachment_id=190,
        parent_message=cast(
            discord.Message,
            FakeMessage(channel=FakeChannel(102), content="failed"),
        ),
        thread=cast(discord.Thread, SimpleNamespace()),
        cleanup_callback=AsyncMock(),
    )
    await no_target_failure.dismiss(AsyncMock(user=SimpleNamespace(id=201)))
    assert no_target_failure.dismissed

    pending = _PendingUploadView(201, 900)
    assert not await pending.interaction_check(AsyncMock(user=SimpleNamespace(id=999)))
    assert await pending.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    check_button = cast(discord.ui.Button[Any], pending.children[0])
    checked = AsyncMock(user=SimpleNamespace(id=201))
    await check_button.callback(checked)
    assert "will not be resubmitted" in checked.response.send_message.call_args.args[0]
