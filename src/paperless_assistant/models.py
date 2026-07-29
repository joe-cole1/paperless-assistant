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
    storage_paths: tuple[TaxonomyItem, ...] = field(default_factory=tuple)


class TaxonomyKind(StrEnum):
    """Paperless taxonomy categories supported by AI review."""

    TAG = "tag"
    CORRESPONDENT = "correspondent"
    DOCUMENT_TYPE = "document_type"
    STORAGE_PATH = "storage_path"


@dataclass(frozen=True, slots=True)
class TaxonomyCapabilities:
    """Creation permissions exposed by Paperless for the invoking user."""

    add_tags: bool = False
    add_correspondents: bool = False
    add_document_types: bool = False
    add_storage_paths: bool = False

    def can_add(self, kind: TaxonomyKind) -> bool:
        """Return whether Paperless allows this user to create ``kind``."""
        return {
            TaxonomyKind.TAG: self.add_tags,
            TaxonomyKind.CORRESPONDENT: self.add_correspondents,
            TaxonomyKind.DOCUMENT_TYPE: self.add_document_types,
            TaxonomyKind.STORAGE_PATH: self.add_storage_paths,
        }[kind]


@dataclass(frozen=True, slots=True)
class MetadataGuidance:
    """Unambiguous metadata selected from an upload caption."""

    tag_ids: tuple[int, ...] = ()
    correspondent_id: int | None = None
    document_type_id: int | None = None


@dataclass(frozen=True, slots=True)
class SuggestedDate:
    """One raw Paperless date candidate and its validated value, when valid."""

    raw: str
    value: date | None


@dataclass(frozen=True, slots=True)
class AISuggestions:
    """Native Paperless LLM suggestions, including names not yet in its taxonomy."""

    title: str | None = None
    correspondent_ids: tuple[int, ...] = field(default_factory=tuple)
    document_type_ids: tuple[int, ...] = field(default_factory=tuple)
    storage_path_ids: tuple[int, ...] = field(default_factory=tuple)
    tag_ids: tuple[int, ...] = field(default_factory=tuple)
    dates: tuple[SuggestedDate, ...] = field(default_factory=tuple)
    suggested_correspondents: tuple[str, ...] = field(default_factory=tuple)
    suggested_document_types: tuple[str, ...] = field(default_factory=tuple)
    suggested_storage_paths: tuple[str, ...] = field(default_factory=tuple)
    suggested_tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SuggestionSelection:
    """Uploader-selected subset of one Paperless AI response."""

    title: str | None = None
    created: date | None = None
    correspondent_id: int | None = None
    document_type_id: int | None = None
    storage_path_id: int | None = None
    tag_ids: tuple[int, ...] = field(default_factory=tuple)
    new_correspondents: tuple[str, ...] = field(default_factory=tuple)
    new_document_types: tuple[str, ...] = field(default_factory=tuple)
    new_storage_paths: tuple[str, ...] = field(default_factory=tuple)
    new_tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SuggestionReview:
    """User-scoped Paperless state required to render and apply one review."""

    document: Document
    suggestions: AISuggestions
    taxonomy: Taxonomy
    capabilities: TaxonomyCapabilities


@dataclass(frozen=True, slots=True)
class DocumentUpdate:
    """Explicit metadata updates to apply to a document."""

    title: str | None = None
    correspondent_id: int | None = None
    document_type_id: int | None = None
    storage_path_id: int | None = None
    created: date | None = None
    tag_ids: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class Document:
    """Privacy-sensitive document metadata used only for Discord responses."""

    id: DocumentId
    title: str
    created: date | None
    original_filename: str | None = None
    archived_filename: str | None = None
    modified: datetime | None = None
    tag_ids: tuple[int, ...] = field(default_factory=tuple)
    correspondent_id: int | None = None
    document_type_id: int | None = None
    storage_path_id: int | None = None


@dataclass(frozen=True, slots=True)
class DiscordMessageTarget:
    """One exact Discord channel/message pair eligible for policy cleanup."""

    channel_id: int
    message_id: int


class UploadItemState(StrEnum):
    """Durable review lifecycle for one Discord attachment."""

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CLOSED = "closed"
    DISMISSED = "dismissed"


RESOLVED_UPLOAD_ITEM_STATES = frozenset({UploadItemState.CLOSED, UploadItemState.DISMISSED})


@dataclass(frozen=True, slots=True)
class UploadBatch:
    """Shared Discord artifacts and owner for one upload message."""

    source_message_id: int
    source_channel_id: int
    summary_message_id: int
    summary_channel_id: int
    principal_id: int
    total_items: int
    source_cleaned: bool = False
    summary_cleaned: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UploadItem:
    """One attachment and its independently reviewable Discord artifacts."""

    source_message_id: int
    attachment_id: int
    ordinal: int
    original_filename: str
    state: UploadItemState = UploadItemState.PENDING
    job_id: UUID | None = None
    document_id: DocumentId | None = None
    parent_message_id: int | None = None
    parent_channel_id: int | None = None
    thread_id: int | None = None
    title_message_id: int | None = None
    metadata_message_id: int | None = None
    actions_message_id: int | None = None
    controls_message_id: int | None = None
    parent_cleaned: bool = False
    thread_cleaned: bool = False
    failure_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UploadBatchSnapshot:
    """One batch and its stable attachment ordering."""

    batch: UploadBatch
    items: tuple[UploadItem, ...]

    @property
    def cleanup_ready(self) -> bool:
        """Return whether user actions resolved every attachment."""
        return bool(self.items) and all(
            item.state in RESOLVED_UPLOAD_ITEM_STATES for item in self.items
        )

    @property
    def cleanup_targets(self) -> tuple[DiscordMessageTarget, ...]:
        """Return undeleted shared source and summary artifacts."""
        targets: list[DiscordMessageTarget] = []
        if not self.batch.summary_cleaned:
            targets.append(
                DiscordMessageTarget(
                    self.batch.summary_channel_id,
                    self.batch.summary_message_id,
                )
            )
        if not self.batch.source_cleaned:
            targets.append(
                DiscordMessageTarget(
                    self.batch.source_channel_id,
                    self.batch.source_message_id,
                )
            )
        return tuple(targets)


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
    duplicate_confirmed: bool = False


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
    discord_message_channel_id: int | None = None
    discord_status_channel_id: int | None = None
    state: JobState = JobState.STAGED
    paperless_task_id: UUID | None = None
    paperless_document_id: DocumentId | None = None
    duplicate_confirmed: bool = False
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
