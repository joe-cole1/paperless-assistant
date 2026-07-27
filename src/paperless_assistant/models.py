"""Transport-neutral domain models for Paperless chat and ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import NewType
from uuid import UUID

DocumentId = NewType("DocumentId", int)


@dataclass(frozen=True, slots=True)
class TaxonomyItem:
    """One visible Paperless taxonomy object."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """Visible taxonomy available to the configured Paperless principal."""

    tags: tuple[TaxonomyItem, ...]
    correspondents: tuple[TaxonomyItem, ...]
    document_types: tuple[TaxonomyItem, ...]


@dataclass(frozen=True, slots=True)
class MetadataGuidance:
    """Unambiguous metadata selected from an upload caption."""

    tag_ids: tuple[int, ...] = ()
    correspondent_id: int | None = None
    document_type_id: int | None = None


@dataclass(frozen=True, slots=True)
class AISuggestions:
    """Native AI suggested metadata for an existing document."""

    title: str | None = None
    correspondent_id: int | None = None
    document_type_id: int | None = None
    tag_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DocumentUpdate:
    """Explicit metadata updates to apply to a document."""

    title: str | None = None
    correspondent_id: int | None = None
    document_type_id: int | None = None
    tag_ids: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class Document:
    """Privacy-sensitive document metadata used only for Discord responses."""

    id: DocumentId
    title: str
    created: date | None
    original_filename: str | None = None
    archived_filename: str | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    """Completed native Paperless chat answer and ordered references."""

    answer: str
    document_ids: tuple[DocumentId, ...]


class TaskState(StrEnum):
    """Paperless task state normalized for application policy."""

    PENDING = "pending"
    STARTED = "started"
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PaperlessTask:
    """Normalized consumption task response."""

    task_id: UUID
    state: TaskState
    document_id: DocumentId | None = None
    message: str | None = None


class JobState(StrEnum):
    """Durable ingestion state."""

    STAGED = "staged"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"


TERMINAL_JOB_STATES = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.RECONCILIATION_REQUIRED}
)

ALLOWED_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.STAGED: frozenset({JobState.SUBMITTING, JobState.FAILED}),
    JobState.SUBMITTING: frozenset(
        {JobState.SUBMITTED, JobState.FAILED, JobState.RECONCILIATION_REQUIRED}
    ),
    JobState.SUBMITTED: frozenset({JobState.SUCCEEDED, JobState.FAILED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.RECONCILIATION_REQUIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class IngestionJob:
    """One independently recoverable Discord attachment ingestion."""

    id: UUID
    discord_message_id: int
    discord_attachment_id: int
    principal_id: int
    staged_path: Path
    original_filename: str
    media_type: str
    office_dependent: bool
    caption: str
    guidance: MetadataGuidance
    discord_status_message_id: int | None = None
    state: JobState = JobState.STAGED
    paperless_task_id: UUID | None = None
    paperless_document_id: DocumentId | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReferenceContext:
    """Short-lived ordered document references for one Discord user."""

    principal_id: int
    document_ids: tuple[DocumentId, ...]
    expires_at: datetime
    source_message_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Download:
    """A restricted temporary file returned by the Paperless gateway."""

    path: Path
    filename: str
    media_type: str
    size: int


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    """Policy result for one requested document delivery."""

    document_id: DocumentId
    attachment: Download | None
    original_url: str
    used_archived: bool = False


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Privacy-minimized durable audit event."""

    principal_id: int
    action: str
    outcome: str
    occurred_at: datetime
    correlation_id: UUID
    job_id: UUID | None = None
    task_id: UUID | None = None
    document_id: DocumentId | None = None
    delivery_method: str | None = None
