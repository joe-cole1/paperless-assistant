"""Discord routing and response tests with synthetic transport objects."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import discord
import pytest

from paperless_assistant.config import Settings
from paperless_assistant.discord_adapter import (
    DiscordAssistant,
    _document_line,
    _is_delivery_request,
    _is_follow_up,
    _result_view,
)
from paperless_assistant.errors import (
    InvalidAttachmentError,
    PaperlessUnavailableError,
    RateLimitedError,
)
from paperless_assistant.models import (
    DeliveryPlan,
    Document,
    DocumentId,
    Download,
    IngestionJob,
    JobState,
    MetadataGuidance,
    ReferenceContext,
)
from paperless_assistant.services import IngestionOutcome, QueryResponse


class FakeChannel:
    def __init__(self, identifier: int) -> None:
        self.id = identifier
        self.sent: list[FakeMessage] = []
        self.partial: list[FakeMessage] = []

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

    async def edit(self, **kwargs: Any) -> None:
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]

    async def delete(self) -> None:
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
        self.error: Exception | None = None
        self.asked: list[tuple[int, str, int | None]] = []
        self.saved: tuple[int, tuple[DocumentId, ...], tuple[int, ...]] | None = None

    async def context(self, principal_id: int) -> ReferenceContext | None:
        assert principal_id == 201
        return self.current_context

    async def ask(
        self,
        principal_id: int,
        question: str,
        *,
        document_id: int | None = None,
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
        lambda _: None,
    )


def _context(*documents: int, message_ids: tuple[int, ...] = ()) -> ReferenceContext:
    return ReferenceContext(
        201,
        tuple(DocumentId(item) for item in documents),
        datetime.now(tz=UTC) + timedelta(minutes=5),
        message_ids,
    )


def test_language_helpers() -> None:
    document = Document(DocumentId(7), "Synthetic", date(2024, 1, 2))

    assert _is_delivery_request("Please send the file")
    assert not _is_delivery_request("Where is it?")
    assert _is_follow_up("What about the date?")
    assert not _is_follow_up("Find a vaccine record")
    assert "2024-01-02" in _document_line(document, "https://example.test")
    assert "date unavailable" in _document_line(
        Document(DocumentId(8), "No Date", None), "https://example.test"
    )


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
    assert len(view.children) == 2

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
    assert question.replies[0].content == "Native answer"
    assert query.asked[-1] == (201, "Find my record", None)
    assert query.saved is not None
    assert channel.sent[-1].content.startswith("**Result 1**")

    query.current_context = _context(7, message_ids=(1234,))
    followup = FakeMessage(channel=channel, content="What about its date?")
    followup.reference = SimpleNamespace(message_id=1234)
    await assistant._questions_message(cast(discord.Message, followup))
    assert query.asked[-1][2] == 7

    query.current_context = _context(7, 8)
    ambiguous = FakeMessage(channel=channel, content="What about the date?")
    await assistant._questions_message(cast(discord.Message, ambiguous))
    assert "Which result" in ambiguous.replies[0].content

    query.current_context = None
    query.error = RateLimitedError("synthetic")
    limited = FakeMessage(channel=channel, content="question")
    await assistant._questions_message(cast(discord.Message, limited))
    assert "quickly" in limited.replies[0].content

    query.error = PaperlessUnavailableError("synthetic")
    unavailable = FakeMessage(channel=channel, content="question")
    await assistant._questions_message(cast(discord.Message, unavailable))
    assert "unavailable" in unavailable.replies[0].content
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
    assert send_second.replies[-1].send_kwargs["file"].filename == "Formatted.pdf"
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
    assert "not available" in unavailable_number.replies[-1].content
    await assistant.close()


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []
        self.deferred = False

    async def send_message(self, content: str, **kwargs: Any) -> None:
        self.messages.append((content, kwargs))

    async def defer(self, **_: Any) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.messages.append((content, kwargs))


class FakeInteraction:
    def __init__(self, settings: Settings, custom_id: str) -> None:
        self.data = {"custom_id": custom_id}
        self.guild_id = settings.discord_guild_id
        self.channel_id = settings.discord_questions_channel_id
        self.user = SimpleNamespace(id=201)
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()
        self.guild = SimpleNamespace(filesize_limit=10 * 1024 * 1024)
        self.filesize_limit = 10 * 1024 * 1024


@pytest.mark.asyncio
async def test_persistent_delivery_interaction(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(data_dir=tmp_path)
    query = FakeQuery()
    delivery = FakeDelivery(tmp_path)
    assistant = _assistant(settings, query, FakeIngestion(), delivery, FakeTaxonomy())

    ignored = FakeInteraction(settings, "other:button")
    await assistant.on_interaction(cast(discord.Interaction, ignored))
    assert not ignored.response.messages

    query.current_context = None
    expired = FakeInteraction(settings, "paperless:send:201:7")
    await assistant.on_interaction(cast(discord.Interaction, expired))
    assert "expired" in expired.response.messages[0][0]

    query.current_context = _context(7)
    valid = FakeInteraction(settings, "paperless:send:201:7")
    await assistant.on_interaction(cast(discord.Interaction, valid))
    assert valid.response.deferred
    assert valid.followup.messages[0][1]["file"].filename == "Formatted.pdf"

    delivery.mode = "link"
    linked = FakeInteraction(settings, "paperless:send:201:7")
    await assistant.on_interaction(cast(discord.Interaction, linked))
    assert "Too large" in linked.followup.messages[0][0]

    delivery.mode = "error"
    failed = FakeInteraction(settings, "paperless:send:201:7")
    await assistant.on_interaction(cast(discord.Interaction, failed))
    assert "unavailable" in failed.followup.messages[0][0]
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
    status = partial.replies[0]
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
    assert ingestion.last_stage_kwargs["discord_status_message_id"] == success.replies[0].id

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
    assert "bad signature" in invalid.replies[0].edits[-1]["content"]

    ingestion.stage_error = None
    ingestion.submit_state = JobState.RECONCILIATION_REQUIRED
    uncertain = FakeMessage(
        channel=channel,
        attachments=[FakeAttachment(4, "four.pdf", b"%PDF-1.7")],
    )
    await assistant._uploads_message(cast(discord.Message, uncertain))
    assert "uncertain" in uncertain.replies[0].edits[-1]["content"]
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

    assert "Tika/Gotenberg" in message.replies[0].edits[-1]["content"]
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
    assert "failed" in channel.sent[-1].content

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

    await assistant.cleanup_messages((10, 11), (12,))
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
        ready.append,
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
