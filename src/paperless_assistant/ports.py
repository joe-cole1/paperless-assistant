"""Application ports implemented by external adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from paperless_assistant.models import (
    AuditEvent,
    ChatResult,
    Document,
    Download,
    IngestionJob,
    JobState,
    MetadataGuidance,
    PaperlessTask,
    ReferenceContext,
    Taxonomy,
)


class PaperlessGateway(Protocol):
    """Narrow Paperless operations required by issue #10."""

    async def chat(self, question: str, document_id: int | None = None) -> ChatResult: ...

    async def search_documents(self, query: str, limit: int = 3) -> tuple[Document, ...]: ...

    async def get_document(self, document_id: int) -> Document: ...

    async def get_taxonomy(self) -> Taxonomy: ...

    async def submit_document(
        self,
        path: Path,
        filename: str,
        media_type: str,
        guidance: MetadataGuidance,
    ) -> UUID: ...

    async def get_task(self, task_id: UUID) -> PaperlessTask: ...

    async def add_note(self, document_id: int, note: str) -> None: ...

    async def download(
        self, document_id: int, destination: Path, *, archived: bool = False
    ) -> Download: ...

    async def get_document_tag_ids(self, document_id: int) -> tuple[int, ...]: ...

    def document_url(self, document_id: int) -> str: ...

    def original_download_url(self, document_id: int) -> str: ...


class IngestionRepository(Protocol):
    """Durable ingestion and short-lived query context storage."""

    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def acquire_instance(self, instance_id: UUID) -> bool: ...

    async def release_instance(self, instance_id: UUID) -> None: ...

    async def create_job(self, job: IngestionJob) -> bool: ...

    async def get_job(self, job_id: UUID) -> IngestionJob | None: ...

    async def transition_job(
        self,
        job_id: UUID,
        expected: JobState,
        target: JobState,
        *,
        task_id: UUID | None = None,
        document_id: int | None = None,
    ) -> bool: ...

    async def recoverable_jobs(self) -> tuple[IngestionJob, ...]: ...

    async def save_context(self, context: ReferenceContext) -> None: ...

    async def get_context(self, principal_id: int) -> ReferenceContext | None: ...

    async def cleanup_message_ids(
        self, *, context_before: str, succeeded_before: str, failed_before: str
    ) -> tuple[tuple[int, ...], tuple[int, ...]]: ...

    async def message_job_states(self, discord_message_id: int) -> tuple[JobState, ...]: ...

    async def active_succeeded_uploads(self) -> tuple[tuple[tuple[int, ...], int], ...]: ...

    async def protected_staged_paths(self) -> frozenset[Path]: ...

    async def get_warning_state(self) -> tuple[int, datetime] | None: ...

    async def save_warning_state(self, message_id: int, emitted_at: datetime) -> None: ...

    async def clear_warning_state(self) -> None: ...

    async def purge(
        self, *, context_before: str, audit_before: str, failed_before: str
    ) -> None: ...


class AuditRepository(Protocol):
    """Privacy-minimized audit storage."""

    async def record(self, event: AuditEvent) -> None: ...

    def actions(self) -> AsyncIterator[str]: ...


class DiscordFileSender(Protocol):
    """Transport boundary used for file delivery."""

    async def send_files(self, downloads: Sequence[Download]) -> None: ...
