"""Discord routing and response tests with synthetic transport objects."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import discord
import pytest
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.discord_adapter import (
    AISuggestionsEditModal,
    AISuggestionsView,
    DiscordAssistant,
    DismissButton,
    _bounded_lines,
    _close_existing_items,
    _ConfirmTaxonomyCreationView,
    _document_embed,
    _is_delivery_request,
    _is_follow_up,
    _MetadataOverflowView,
    _MetadataPageSelect,
    _MetadataSelect,
    _result_view,
)
from paperless_assistant.errors import (
    InvalidAttachmentError,
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
    SuggestionReview,
    SuggestionSelection,
    Taxonomy,
    TaxonomyCapabilities,
    TaxonomyItem,
    TaxonomyKind,
)
from paperless_assistant.services import IngestionOutcome, IngestionService, QueryResponse


class FakeChannel:
    def __init__(self, identifier: int) -> None:
        self.id = identifier
        self.sent: list[FakeMessage] = []
        self.partial: list[FakeMessage] = []
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
        return ()


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
        self.name = name
        self.sent: list[FakeMessage] = []
        self.guild = cast(discord.Guild, SimpleNamespace(id=100, filesize_limit=10 * 1024 * 1024))

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        message = FakeMessage(
            channel=cast(Any, self),
            content=content or "",
            identifier=1000 + len(self.sent),
        )
        message.send_kwargs = kwargs
        self.sent.append(message)
        return message


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
        self.error: Exception | None = None
        self.asked: list[tuple[int, str, int | None]] = []
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
        self.poll_outcome: IngestionOutcome | None = None
        self.recovered = False
        self.office_dependent = False
        self.last_stage_kwargs: dict[str, Any] = {}
        self.warning_marker: tuple[int, datetime] | None = None
        self.suggestions_error: Exception | None = None

    async def stage(self, **kwargs: Any) -> IngestionJob | None:
        self.last_stage_kwargs = kwargs
        if self.stage_error:
            raise self.stage_error
        if self.stage_duplicate:
            return None
        return IngestionJob(
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

    async def submit(self, job: IngestionJob) -> IngestionOutcome:
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
            state=self.submit_state,
            paperless_task_id=uuid4(),
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
    ) -> None:
        del expected_modified
        self.applied_updates = updates

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
    view = _result_view(
        201, 7, "https://paperless.example.test/doc", settings.discord_allowed_user_ids
    )
    assert len(view.children) == 3

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
    status = partial.thread.sent[0]
    combined = "\n".join(edit["content"] for edit in status.edits)
    assert "too large" in combined
    assert "staging quota" in combined
    assert "uploaded as" in combined
    assert not partial.deleted
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
    assert success.deleted
    assert ingestion.last_stage_kwargs["discord_status_message_id"] == success.thread.sent[0].id

    ingestion.stage_duplicate = True
    duplicate = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(2, "two.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, duplicate))
    assert duplicate.deleted

    ingestion.stage_duplicate = False
    ingestion.stage_error = InvalidAttachmentError("bad signature")
    invalid = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(3, "three.pdf", b"bad")],
    )
    await assistant._uploads_message(cast(discord.Message, invalid))
    assert "bad signature" in invalid.thread.sent[0].edits[-1]["content"]

    ingestion.stage_error = None
    ingestion.submit_state = JobState.RECONCILIATION_REQUIRED
    uncertain = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(4, "four.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, uncertain))
    assert "uncertain" in uncertain.thread.sent[0].edits[-1]["content"]
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
    assert "link your Paperless" in unlinked_stage.thread.sent[0].edits[-1]["content"]

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
        combined = "\n".join(edit["content"] for edit in message.thread.sent[0].edits)
        assert "AI suggestions are unavailable" in combined

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
    assert "Only the first 1" in too_many.thread.sent[0].edits[-1]["content"]

    actual_too_large = FakeMessage(
        channel=channel,
        attachments=[
            FakeAttachment(3, "large.pdf", b"01234567890", declared_size=1),
        ],
    )
    await assistant._uploads_message(cast(discord.Message, actual_too_large))
    assert "downloaded file exceeds" in actual_too_large.thread.sent[0].edits[-1]["content"]

    failed_download = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(4, "failed.pdf", b"x", fail=True)],
    )
    await assistant._uploads_message(cast(discord.Message, failed_download))
    assert "download failed" in failed_download.thread.sent[0].edits[-1]["content"]

    ingestion.submit_state = JobState.FAILED
    rejected = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(5, "rejected.pdf", b"%PDF")],
    )
    await assistant._uploads_message(cast(discord.Message, rejected))
    assert "rejected" in rejected.thread.sent[0].edits[-1]["content"]
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
    assert "staging quota was exceeded" in quota.thread.sent[0].edits[-1]["content"]

    existing.unlink()
    cast(Any, ingestion).poll_until_notifiable = AsyncMock(side_effect=RuntimeError("synthetic"))
    unavailable = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(2, "unavailable.pdf", b"%PDF")],
    )
    await assistant._uploads_message(cast(discord.Message, unavailable))
    assert "status unavailable" in unavailable.thread.sent[0].edits[-1]["content"]

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
    assert "still processing" in timed_out.thread.sent[0].edits[-1]["content"]
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

    assert "Tika/Gotenberg" in message.thread.sent[0].edits[-1]["content"]
    assert "macro detected" not in message.thread.sent[0].edits[-1]["content"]
    await assistant.close()


@pytest.mark.asyncio
async def test_warning_recovery_and_status_helpers(
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
async def test_dismiss_button_callback(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    button = DismissButton(settings.discord_allowed_user_ids)
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
async def test_clean_command_callback(
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

    interaction_non_text = AsyncMock()
    interaction_non_text.user = SimpleNamespace(id=201)
    interaction_non_text.channel_id = settings.discord_questions_channel_id
    interaction_non_text.channel = "invalid"
    await callback(cast(discord.Interaction, interaction_non_text), 10)
    interaction_non_text.followup.send.assert_awaited_with("Invalid channel type.", ephemeral=True)

    monkeypatch.setattr(discord, "TextChannel", FakeChannel)
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
    assert "uploaded as" in u_thread.sent[0].content

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
    assert "have not linked your Paperless account" in msg_unlinked_u.thread.sent[0].content

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


@pytest.mark.asyncio
async def test_ai_suggestions_view_interactions(  # noqa: PLR0915
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    _ = settings_factory(data_dir=tmp_path)
    job = IngestionJob(
        id=uuid4(),
        discord_message_id=1,
        discord_attachment_id=2,
        discord_status_message_id=3,
        principal_id=201,
        staged_path=tmp_path / "synthetic.pdf",
        original_filename="synthetic.pdf",
        caption="",
        media_type="application/pdf",
        office_dependent=False,
        guidance=MetadataGuidance((), None, None),
    )
    document = Document(
        DocumentId(7),
        "Synthetic",
        date(2024, 1, 2),
        modified=datetime(2026, 7, 28, tzinfo=UTC),
    )
    suggestions = AISuggestions(
        title="Suggested",
        correspondent_ids=(1,),
        document_type_ids=(1,),
        tag_ids=(2, 3, 99),
        dates=(SuggestedDate("2026-07-28", date(2026, 7, 28)),),
        suggested_tags=("New Tag",),
    )
    review = SuggestionReview(
        document,
        suggestions,
        FakeTaxonomy().snapshot,
        TaxonomyCapabilities(True, True, True, True),
    )

    ingestion = FakeIngestion()
    ingestion.apply_suggestions = AsyncMock()  # type: ignore[method-assign]

    view = AISuggestionsView(job, review, cast(Any, ingestion), 900)

    # Cross-user interactions are rejected before Discord dispatches a button callback.
    unauth_interaction = AsyncMock(user=SimpleNamespace(id=999))
    assert not await view.interaction_check(unauth_interaction)
    assert "Only the user who uploaded" in unauth_interaction.response.send_message.call_args[0][0]
    assert await view.interaction_check(AsyncMock(user=SimpleNamespace(id=201)))
    assert view._selected_option_values(TaxonomyKind.CORRESPONDENT) == ("id:1",)

    # Authorized approve
    auth_interaction = AsyncMock(user=SimpleNamespace(id=201))
    auth_interaction.message = SimpleNamespace(
        embeds=[discord.Embed(title="Old")], edit=AsyncMock()
    )
    await view.approve_button.callback(auth_interaction)
    ingestion.apply_suggestions.assert_called_once()
    assert auth_interaction.message.edit.call_args[1]["embed"].title == "✅ Applied to Synthetic"

    already_applied = AsyncMock(user=SimpleNamespace(id=201), message=None)
    await view.approve_button.callback(already_applied)
    assert "already applied" in already_applied.followup.send.call_args[0][0]

    # Edit modal authorized
    edit_interaction = AsyncMock(user=SimpleNamespace(id=201))
    await view.edit_button.callback(edit_interaction)
    modal = edit_interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal, AISuggestionsEditModal)

    # Submit modal
    modal.title_input._value = "User Edited Title"
    modal_submit_interaction = AsyncMock(user=SimpleNamespace(id=201))
    modal_submit_interaction.message = SimpleNamespace(
        embeds=[discord.Embed().add_field(name="Suggested Title", value="Suggested")],
    )
    modal_submit_interaction.response.edit_message = AsyncMock()
    await modal.on_submit(modal_submit_interaction)
    assert view.current_title == "User Edited Title"
    assert (
        "User Edited Title"
        in modal_submit_interaction.response.edit_message.call_args[1]["embed"].fields[0].value
    )

    modal.date_input._value = "not-a-date"
    invalid_date_interaction = AsyncMock()
    await modal.on_submit(invalid_date_interaction)
    assert "valid date" in invalid_date_interaction.response.send_message.call_args.args[0]
    modal.date_input._value = ""

    unauthorized_modal = AsyncMock(user=SimpleNamespace(id=999))
    await modal.on_submit(unauthorized_modal)
    assert "Only the uploader" in unauthorized_modal.response.send_message.call_args.args[0]

    # Cancel authorized
    cancel_interaction = AsyncMock(user=SimpleNamespace(id=201))
    cancel_interaction.message = AsyncMock()
    await view.cancel_button.callback(cancel_interaction)
    cancel_interaction.message.delete.assert_called_once()

    # Unexpected application failures stay generic.
    error_view = AISuggestionsView(job, review, cast(Any, ingestion), 900)
    ingestion.apply_suggestions.side_effect = Exception("Test Error")
    auth_interaction_err = AsyncMock(
        user=SimpleNamespace(id=201), message=SimpleNamespace(embeds=[discord.Embed()])
    )
    await error_view.approve_button.callback(auth_interaction_err)
    assert "Test Error" not in auth_interaction_err.followup.send.call_args[0][0]

    for error, expected in (
        (StaleSuggestionError("stale"), "changed after"),
        (UnlinkedUserError("unlinked"), "no longer linked"),
        (PaperlessUnavailableError("unavailable"), "could not resolve"),
    ):
        ingestion.apply_suggestions = AsyncMock(side_effect=error)  # type: ignore[method-assign]
        failed_view = AISuggestionsView(job, review, cast(Any, ingestion), 900)
        failed_apply = AsyncMock(user=SimpleNamespace(id=201), message=None)
        await failed_view.approve_button.callback(failed_apply)
        assert expected in failed_apply.followup.send.call_args.args[0]

    ingestion.apply_suggestions = AsyncMock()  # type: ignore[method-assign]
    no_source_view = AISuggestionsView(job, review, cast(Any, ingestion), 900)
    no_source_apply = AsyncMock()
    await no_source_view.apply(no_source_apply, None, confirm_create=False)
    assert "confirmed" in no_source_apply.followup.send.call_args.args[0]

    # Coverage: modal_submit with no message
    modal_submit_interaction_no_msg = AsyncMock(
        user=SimpleNamespace(id=201),
        message=None,
    )
    await modal.on_submit(modal_submit_interaction_no_msg)
    modal_submit_interaction_no_msg.response.defer.assert_called_once()

    # Refresh authorized
    auth_refresh = AsyncMock(user=SimpleNamespace(id=201))
    auth_refresh.message = SimpleNamespace(edit=AsyncMock())
    await view.refresh_button.callback(auth_refresh)
    auth_refresh.message.edit.assert_called_once()

    for error, expected in (
        (StaleSuggestionError("stale"), "changed while"),
        (UnlinkedUserError("unlinked"), "no longer linked"),
        (PaperlessUnavailableError("unavailable"), "unavailable"),
    ):
        ingestion.get_suggestion_review = AsyncMock(side_effect=error)  # type: ignore[method-assign]
        failed_refresh = AsyncMock(user=SimpleNamespace(id=201), message=None)
        await view.refresh_button.callback(failed_refresh)
        assert expected in failed_refresh.followup.send.call_args.args[0]
    ingestion.get_suggestion_review = AsyncMock(return_value=None)  # type: ignore[method-assign]
    empty_refresh = AsyncMock(user=SimpleNamespace(id=201), message=None)
    await view.refresh_button.callback(empty_refresh)
    assert "cached" in empty_refresh.followup.send.call_args.args[0]

    # Coverage: cancel_button with no message
    cancel_interaction_no_msg = AsyncMock(user=SimpleNamespace(id=201), message=None)
    await view.cancel_button.callback(cancel_interaction_no_msg)


@pytest.mark.asyncio
async def test_ai_suggestion_combined_selectors_and_pagination(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
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
        Document(
            DocumentId(7),
            "Current",
            None,
            modified=datetime(2026, 7, 28, tzinfo=UTC),
            tag_ids=(2,),
            correspondent_id=1,
            document_type_id=3,
            storage_path_id=4,
        ),
        suggestions,
        taxonomy,
        TaxonomyCapabilities(True, True, True, True),
    )
    ingestion = FakeIngestion()
    view = AISuggestionsView(job, review, cast(Any, ingestion), 900)
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
    assert selects[TaxonomyKind.TAG].options[-1].value == "more"
    assert "32 choices" in selects[TaxonomyKind.TAG].options[-1].label
    assert "☐ Close existing · Mary Smith (for AI: Mary Smyth)" in str(
        view.build_embed().fields[2].value
    )

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
    source_message.edit.assert_awaited()

    overflow_without_source = _MetadataOverflowView(
        view,
        TaxonomyKind.TAG,
        None,
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
    assert "☑ Close existing · New Tag Zero (for AI: New Tag 0)" in str(
        view.build_embed().fields[5].value
    )

    assert _bounded_lines(()) == "None"
    assert _bounded_lines(("x" * 1100,)).endswith("…")
    close = _close_existing_items(
        ("Mary Smit", "Mary Smith"),
        taxonomy.correspondents,
        (),
    )
    assert close == ((TaxonomyItem(9, "Mary Smith"), "Mary Smith"),)
    assert _close_existing_items(
        ("Mary Smith", "Mary Smit"),
        taxonomy.correspondents,
        (),
    ) == ((TaxonomyItem(9, "Mary Smith"), "Mary Smith"),)


@pytest.mark.asyncio
async def test_ai_new_taxonomy_requires_second_confirmation(tmp_path: Path) -> None:
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
    review = SuggestionReview(
        Document(
            DocumentId(7),
            "Current",
            date(2026, 7, 1),
            modified=datetime(2026, 7, 28, tzinfo=UTC),
        ),
        AISuggestions(
            suggested_correspondents=("Abby",),
            suggested_document_types=("Text Message",),
            suggested_storage_paths=("Messages/Mary",),
            suggested_tags=("new-topic",),
        ),
        Taxonomy((), (), (), ()),
        TaxonomyCapabilities(True, True, True, True),
    )
    ingestion = FakeIngestion()
    ingestion.apply_suggestions = AsyncMock()  # type: ignore[method-assign]
    ingestion.resolve_or_create_taxonomy = AsyncMock(  # type: ignore[method-assign]
        side_effect=(
            TaxonomyItem(101, "new-topic"),
            TaxonomyItem(102, "Abby"),
            TaxonomyItem(103, "Text Message"),
            TaxonomyItem(104, "Messages/Mary"),
        )
    )
    view = AISuggestionsView(job, review, cast(Any, ingestion), 900)
    view.update_selection(TaxonomyKind.TAG, ("new:0",))
    view.update_selection(TaxonomyKind.CORRESPONDENT, ("new:0",))
    view.update_selection(TaxonomyKind.DOCUMENT_TYPE, ("new:0",))
    view.update_selection(TaxonomyKind.STORAGE_PATH, ("new:0",))

    source_message = AsyncMock(spec=discord.Message)
    approve = AsyncMock(user=SimpleNamespace(id=201), message=source_message)
    await view.approve_button.callback(approve)
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
    source_message.edit.assert_awaited_once()
