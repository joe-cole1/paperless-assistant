"""Transport-neutral application services and workflow policy."""

from __future__ import annotations

import asyncio
import shutil
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    AmbiguousSubmissionError,
    ConfigurationUnavailableError,
    PaperlessUnavailableError,
    RateLimitedError,
    UnlinkedUserError,
)
from paperless_assistant.models import (
    AISuggestions,
    AuditEvent,
    ChatResult,
    DeliveryPlan,
    Document,
    DocumentId,
    DocumentUpdate,
    IngestionJob,
    JobState,
    MetadataGuidance,
    ReferenceContext,
    TaskState,
    Taxonomy,
    TaxonomyItem,
)
from paperless_assistant.paperless import PAPERLESS_CHAT_ERROR, PAPERLESS_NO_CONTENT
from paperless_assistant.policy import find_required_tag, resolve_taxonomy, validate_attachment
from paperless_assistant.ports import (
    AuditRepository,
    CredentialRepository,
    IngestionRepository,
    PaperlessGateway,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class QueryResponse:
    """One native chat or basic-search response for the Discord adapter."""

    answer: str
    documents: tuple[Document, ...]
    used_search_fallback: bool
    correlation_id: UUID


class QuestionRateLimiter:
    """In-memory per-user sliding window around native AI calls."""

    def __init__(self, limit: int, window: timedelta) -> None:
        self._limit = limit
        self._window = window
        self._calls: defaultdict[int, deque[datetime]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, principal_id: int, now: datetime | None = None) -> None:
        """Consume one allowance or fail without queueing unbounded work."""
        current = now or _now()
        cutoff = current - self._window
        async with self._lock:
            calls = self._calls[principal_id]
            while calls and calls[0] <= cutoff:
                calls.popleft()
            if len(calls) >= self._limit:
                raise RateLimitedError("native chat rate exceeded")
            calls.append(current)


class QueryService:
    """Native Paperless chat with bounded search fallback and short context."""

    def __init__(
        self,
        settings: Settings,
        gateway: PaperlessGateway,
        repository: IngestionRepository,
        audit: AuditRepository,
        credentials: CredentialRepository | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._repository = repository
        self._audit = audit
        self._credentials = credentials
        self._semaphore = asyncio.Semaphore(settings.question_global_concurrency)
        self._rate = QuestionRateLimiter(
            settings.question_user_rate_limit,
            timedelta(seconds=settings.question_user_rate_window_seconds),
        )

    async def ask(
        self,
        principal_id: int,
        question: str,
        *,
        document_id: int | None = None,
        context_id: int | None = None,
    ) -> QueryResponse:
        """Ask Paperless unchanged and fetch only its ordered references."""
        user_token = (
            await self._credentials.get_user_token(principal_id)
            if self._credentials is not None
            else None
        )
        if self._credentials is not None and user_token is None:
            raise UnlinkedUserError("Paperless account is not linked")
        await self._rate.acquire(principal_id)
        correlation_id = uuid4()
        used_fallback = False
        target_context_id = context_id if context_id is not None else principal_id
        try:
            async with self._semaphore:
                result = await self._gateway.chat(question, document_id, token=user_token)
        except PaperlessUnavailableError:
            result = ChatResult("", ())
        if not result.answer.strip() or result.answer.strip() in {
            PAPERLESS_NO_CONTENT,
            PAPERLESS_CHAT_ERROR,
        }:
            used_fallback = True
            documents = await self._gateway.search_documents(question, 3, token=user_token)
            answer = "Paperless chat was unavailable; these are basic full-text search results."
        else:
            answer = result.answer
            documents = tuple(
                [
                    await self._gateway.get_document(int(identifier), token=user_token)
                    for identifier in result.document_ids
                ]
            )
        if documents:
            await self._repository.save_context(
                ReferenceContext(
                    principal_id=target_context_id,
                    document_ids=tuple(document.id for document in documents[:3]),
                    expires_at=_now() + self._settings.context_ttl,
                )
            )
        await self._audit.record(
            AuditEvent(
                principal_id=principal_id,
                action="question",
                outcome="search_fallback" if used_fallback else "answered",
                occurred_at=_now(),
                correlation_id=correlation_id,
            )
        )
        return QueryResponse(answer, documents[:3], used_fallback, correlation_id)

    async def context(self, principal_id: int) -> ReferenceContext | None:
        """Read one user's unexpired ordinal context."""
        return await self._repository.get_context(principal_id)

    async def save_rendered_context(
        self,
        principal_id: int,
        document_ids: tuple[DocumentId, ...],
        result_message_ids: tuple[int, ...],
    ) -> None:
        """Persist the result-message mapping used for reply targeting."""
        await self._repository.save_context(
            ReferenceContext(
                principal_id=principal_id,
                document_ids=document_ids,
                source_message_ids=result_message_ids,
                expires_at=_now() + self._settings.context_ttl,
            )
        )


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """Latest observable state for one attachment."""

    job: IngestionJob
    document: Document | None = None
    note_failed: bool = False
    notification_timed_out: bool = False


class TaxonomyCache:
    """Refresh visible taxonomy and gate ingestion on one exact source tag."""

    def __init__(self, settings: Settings, gateway: PaperlessGateway) -> None:
        self._settings = settings
        self._gateway = gateway
        self._taxonomy: Taxonomy | None = None
        self._required_tag: TaxonomyItem | None = None
        self._lock = asyncio.Lock()

    @property
    def ingestion_ready(self) -> bool:
        return self._taxonomy is not None and self._required_tag is not None

    @property
    def snapshot(self) -> Taxonomy | None:
        """Access the latest visible taxonomy snapshot."""
        return self._taxonomy

    async def refresh(self) -> bool:
        """Refresh atomically; an unavailable or ambiguous tag fails closed."""
        async with self._lock:
            try:
                taxonomy = await self._gateway.get_taxonomy()
            except PaperlessUnavailableError:
                self._taxonomy = None
                self._required_tag = None
                return False
            self._taxonomy = taxonomy
            self._required_tag = find_required_tag(taxonomy, self._settings.paperless_source_tag)
            return self._required_tag is not None

    def guidance(self, caption: str) -> MetadataGuidance:
        """Resolve a caption against the most recent safe taxonomy snapshot."""
        if self._taxonomy is None or self._required_tag is None:
            raise ConfigurationUnavailableError("required Paperless source tag is unavailable")
        return resolve_taxonomy(caption, self._taxonomy, self._required_tag)


class IngestionService:
    """Durable immediate-ingestion workflow with fail-closed POST recovery."""

    def __init__(  # noqa: PLR0913
        self,
        settings: Settings,
        gateway: PaperlessGateway,
        repository: IngestionRepository,
        audit: AuditRepository,
        taxonomy: TaxonomyCache,
        *,
        credentials: CredentialRepository | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._repository = repository
        self._audit = audit
        self._taxonomy = taxonomy
        self._credentials = credentials

    async def stage(  # noqa: PLR0913
        self,
        *,
        discord_message_id: int,
        discord_attachment_id: int,
        principal_id: int,
        staged_path: Path,
        original_filename: str,
        caption: str,
        discord_status_message_id: int | None = None,
    ) -> IngestionJob | None:
        """Validate and persist one already-downloaded attachment idempotently."""
        media_type, office_dependent = validate_attachment(
            staged_path,
            original_filename,
            office_enabled=self._settings.paperless_office_uploads_enabled,
        )
        job = IngestionJob(
            id=uuid4(),
            discord_message_id=discord_message_id,
            discord_attachment_id=discord_attachment_id,
            discord_status_message_id=discord_status_message_id,
            principal_id=principal_id,
            staged_path=staged_path,
            original_filename=original_filename,
            media_type=media_type,
            office_dependent=office_dependent,
            caption=caption,
            guidance=self._taxonomy.guidance(caption),
        )
        if not await self._repository.create_job(job):
            return None
        await self._record(job, "staged")
        return job

    async def submit(self, job: IngestionJob) -> IngestionOutcome:
        """Submit a staged job exactly once and persist its task UUID."""
        user_token = (
            await self._credentials.get_user_token(job.principal_id)
            if self._credentials is not None
            else None
        )
        if self._credentials is not None and user_token is None:
            raise UnlinkedUserError("Paperless account is not linked")
        transitioned = await self._repository.transition_job(
            job.id, JobState.STAGED, JobState.SUBMITTING
        )
        if not transitioned:
            current = await self._repository.get_job(job.id)
            if current is None:
                raise RuntimeError("ingestion job disappeared")
            return IngestionOutcome(current)
        submitting = await self._required_job(job.id)
        await self._record(submitting, "submitting")
        try:
            task_id = await self._gateway.submit_document(
                job.staged_path,
                job.original_filename,
                job.media_type,
                job.guidance,
                token=user_token,
            )
        except AmbiguousSubmissionError:
            await self._repository.transition_job(
                job.id,
                JobState.SUBMITTING,
                JobState.RECONCILIATION_REQUIRED,
            )
            current = await self._required_job(job.id)
            await self._record(current, "reconciliation_required")
            return IngestionOutcome(current)
        except PaperlessUnavailableError:
            await self._repository.transition_job(job.id, JobState.SUBMITTING, JobState.FAILED)
            job.staged_path.unlink(missing_ok=True)
            current = await self._required_job(job.id)
            await self._record(current, "failed")
            return IngestionOutcome(current)
        await self._repository.transition_job(
            job.id, JobState.SUBMITTING, JobState.SUBMITTED, task_id=task_id
        )
        current = await self._required_job(job.id)
        await self._record(current, "submitted")
        return IngestionOutcome(current)

    async def poll_until_notifiable(self, job: IngestionJob) -> IngestionOutcome:
        """Poll through the user notification bound; recovery continues later."""
        timeout_seconds = (
            self._settings.paperless_office_task_notification_timeout_seconds
            if job.office_dependent
            else self._settings.paperless_native_task_notification_timeout_seconds
        )
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        delay = self._settings.paperless_task_poll_initial_seconds
        current = job
        while current.state == JobState.SUBMITTED:
            outcome = await self.poll_once(current)
            current = outcome.job
            if current.state != JobState.SUBMITTED:
                return outcome
            if asyncio.get_running_loop().time() >= deadline:
                return IngestionOutcome(current, notification_timed_out=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._settings.paperless_task_poll_max_seconds)
        return IngestionOutcome(current)

    async def poll_once(self, job: IngestionJob) -> IngestionOutcome:
        """Poll one saved task UUID without resubmitting document bytes."""
        if job.state != JobState.SUBMITTED or job.paperless_task_id is None:
            return IngestionOutcome(job)
        user_token = (
            await self._credentials.get_user_token(job.principal_id)
            if self._credentials is not None
            else None
        )
        task = await self._gateway.get_task(job.paperless_task_id, token=user_token)
        if task.state in {TaskState.PENDING, TaskState.STARTED, TaskState.UNKNOWN}:
            return IngestionOutcome(job)
        if task.state == TaskState.FAILURE or task.document_id is None:
            await self._repository.transition_job(job.id, JobState.SUBMITTED, JobState.FAILED)
            job.staged_path.unlink(missing_ok=True)
            current = await self._required_job(job.id)
            await self._record(current, "failed")
            return IngestionOutcome(current)
        note_failed = False
        if job.caption:
            try:
                await self._gateway.add_note(
                    int(task.document_id),
                    f"Discord upload guidance: {job.caption}",
                    token=user_token,
                )
            except PaperlessUnavailableError:
                note_failed = True
        await self._repository.transition_job(
            job.id,
            JobState.SUBMITTED,
            JobState.SUCCEEDED,
            document_id=int(task.document_id),
        )
        job.staged_path.unlink(missing_ok=True)
        current = await self._required_job(job.id)
        document = await self._gateway.get_document(int(task.document_id), token=user_token)
        await self._record(current, "succeeded_note_failed" if note_failed else "succeeded")
        return IngestionOutcome(current, document, note_failed)

    async def recover(
        self, notify: Callable[[IngestionOutcome], Awaitable[None]] | None = None
    ) -> None:
        """Resume safe states and quarantine every interrupted POST window."""
        for job in await self._repository.recoverable_jobs():
            if job.state == JobState.SUBMITTING:
                await self._repository.transition_job(
                    job.id,
                    JobState.SUBMITTING,
                    JobState.RECONCILIATION_REQUIRED,
                )
                outcome = IngestionOutcome(await self._required_job(job.id))
            elif job.state == JobState.STAGED:
                outcome = await self.submit(job)
            else:
                try:
                    outcome = await self.poll_once(job)
                except PaperlessUnavailableError:
                    continue
            if notify is not None and outcome.job.state != JobState.SUBMITTED:
                await notify(outcome)

    async def message_succeeded(self, discord_message_id: int) -> bool:
        """Return true only when every durable attachment job succeeded."""
        states = await self._repository.message_job_states(discord_message_id)
        return bool(states) and all(state == JobState.SUCCEEDED for state in states)

    async def warning_state(self) -> tuple[int, datetime] | None:
        """Return the durable missing-tag warning marker."""
        return await self._repository.get_warning_state()

    async def record_warning(self, message_id: int, emitted_at: datetime) -> None:
        """Record the newest missing-tag warning without its text."""
        await self._repository.save_warning_state(message_id, emitted_at)

    async def clear_warning(self) -> None:
        """Forget a warning after it is removed from Discord."""
        await self._repository.clear_warning_state()

    async def _required_job(self, job_id: UUID) -> IngestionJob:
        job = await self._repository.get_job(job_id)
        if job is None:
            raise RuntimeError("ingestion job disappeared")
        return job

    async def check_inbox_tag_removals(self) -> tuple[int, ...]:
        """Return message IDs for active upload notifications
        whose inbox tag was removed in Paperless.
        """
        if not self._settings.cleanup_inbox_tag_enabled:
            return ()
        active_uploads = await self._repository.active_succeeded_uploads()
        if not active_uploads:
            return ()
        try:
            taxonomy = await self._gateway.get_taxonomy()
        except PaperlessUnavailableError:
            return ()
        inbox_tag_name = self._settings.cleanup_inbox_tag.casefold()
        inbox_tag = next(
            (tag for tag in taxonomy.tags if tag.name.casefold() == inbox_tag_name),
            None,
        )
        if inbox_tag is None:
            return ()
        removed_message_ids: list[int] = []
        for msg_ids, doc_id in active_uploads:
            doc_tag_ids = await self._gateway.get_document_tag_ids(doc_id)
            if inbox_tag.id not in doc_tag_ids:
                removed_message_ids.extend(msg_ids)
        return tuple(dict.fromkeys(removed_message_ids))

    async def _record(self, job: IngestionJob, outcome: str) -> None:
        await self._audit.record(
            AuditEvent(
                principal_id=job.principal_id,
                action="ingestion",
                outcome=outcome,
                occurred_at=_now(),
                correlation_id=uuid4(),
                job_id=job.id,
                task_id=job.paperless_task_id,
                document_id=job.paperless_document_id,
            )
        )

    async def get_suggestions_for_job(self, job: IngestionJob) -> AISuggestions | None:
        """Fetch AI suggestions for a completed job."""
        if job.paperless_document_id is None:
            return None
        user_token = (
            await self._credentials.get_user_token(job.principal_id)
            if self._credentials is not None
            else None
        )
        if self._credentials is not None and user_token is None:
            raise UnlinkedUserError("Paperless account is not linked")
        try:
            return await self._gateway.get_ai_suggestions(
                int(job.paperless_document_id), token=user_token
            )
        except PaperlessUnavailableError:
            return None

    async def apply_suggestions(self, job: IngestionJob, updates: DocumentUpdate) -> None:
        """Apply user-approved suggestions to the document."""
        if job.paperless_document_id is None:
            return
        user_token = (
            await self._credentials.get_user_token(job.principal_id)
            if self._credentials is not None
            else None
        )
        if self._credentials is not None and user_token is None:
            raise UnlinkedUserError("Paperless account is not linked")
        await self._gateway.update_document(
            int(job.paperless_document_id), updates, token=user_token
        )
        await self._record(job, "suggestions_applied")


class DeliveryService:
    """Spool bounded Paperless downloads and select attachment versus link."""

    def __init__(
        self,
        settings: Settings,
        gateway: PaperlessGateway,
        audit: AuditRepository,
        credentials: CredentialRepository | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._audit = audit
        self._credentials = credentials

    async def prepare(
        self, principal_id: int, document_id: int, attachment_limit: int
    ) -> DeliveryPlan:
        """Prefer latest original, then archived PDF, then authenticated link."""
        user_token = (
            await self._credentials.get_user_token(principal_id)
            if self._credentials is not None
            else None
        )
        if self._credentials is not None and user_token is None:
            raise UnlinkedUserError("Paperless account is not linked")
        if (
            shutil.disk_usage(self._settings.delivery_dir).free
            < self._settings.delivery_min_free_bytes
        ):
            return DeliveryPlan(
                DocumentId(document_id),
                None,
                self._gateway.original_download_url(document_id),
            )
        original_path = self._settings.delivery_dir / str(uuid4())
        original = await self._gateway.download(
            document_id, original_path, archived=False, token=user_token
        )
        if original.size <= attachment_limit:
            plan = DeliveryPlan(
                DocumentId(document_id),
                original,
                self._gateway.original_download_url(document_id),
            )
            await self._record(principal_id, document_id, "original_attachment")
            return plan
        original.path.unlink(missing_ok=True)
        archived_path = self._settings.delivery_dir / str(uuid4())
        try:
            archived = await self._gateway.download(
                document_id, archived_path, archived=True, token=user_token
            )
        except PaperlessUnavailableError:
            archived = None
        if archived is not None and archived.size <= attachment_limit:
            plan = DeliveryPlan(
                DocumentId(document_id),
                archived,
                self._gateway.original_download_url(document_id),
                used_archived=True,
            )
            await self._record(principal_id, document_id, "archived_attachment")
            return plan
        if archived is not None:
            archived.path.unlink(missing_ok=True)
        await self._record(principal_id, document_id, "authenticated_link")
        return DeliveryPlan(
            DocumentId(document_id),
            None,
            self._gateway.original_download_url(document_id),
        )

    async def _record(self, principal_id: int, document_id: int, method: str) -> None:
        await self._audit.record(
            AuditEvent(
                principal_id=principal_id,
                action="delivery",
                outcome="prepared",
                occurred_at=_now(),
                correlation_id=uuid4(),
                document_id=DocumentId(document_id),
                delivery_method=method,
            )
        )

    @staticmethod
    def cleanup(plan: DeliveryPlan) -> None:
        """Remove spooled bytes immediately after delivery or failure."""
        if plan.attachment is not None:
            plan.attachment.path.unlink(missing_ok=True)
