"""SQLite WAL repositories for durable jobs, context, and minimized audit."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet
from pydantic import SecretStr

from paperless_assistant.models import (
    ALLOWED_JOB_TRANSITIONS,
    AuditEvent,
    DiscordMessageTarget,
    DocumentId,
    IngestionJob,
    JobState,
    MetadataGuidance,
    ReferenceContext,
    ReviewFinalizationState,
    UploadBatch,
    UploadBatchSnapshot,
    UploadItem,
    UploadItemState,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS user_credentials (
    principal_id INTEGER PRIMARY KEY,
    encrypted_token BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_lease (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    instance_id TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id TEXT PRIMARY KEY,
    discord_message_id INTEGER NOT NULL,
    discord_attachment_id INTEGER NOT NULL,
    discord_status_message_id INTEGER,
    discord_message_channel_id INTEGER,
    discord_status_channel_id INTEGER,
    discord_message_cleaned INTEGER NOT NULL DEFAULT 0,
    discord_status_message_cleaned INTEGER NOT NULL DEFAULT 0,
    principal_id INTEGER NOT NULL,
    staged_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    office_dependent INTEGER NOT NULL,
    caption TEXT NOT NULL,
    guidance_json TEXT NOT NULL,
    state TEXT NOT NULL,
    paperless_task_id TEXT,
    paperless_document_id INTEGER,
    duplicate_confirmed INTEGER NOT NULL DEFAULT 0,
    review_finalization_state TEXT NOT NULL DEFAULT 'not_started',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (discord_message_id, discord_attachment_id)
);

CREATE INDEX IF NOT EXISTS ingestion_jobs_state_idx ON ingestion_jobs(state);
CREATE INDEX IF NOT EXISTS ingestion_jobs_updated_idx ON ingestion_jobs(updated_at);

CREATE TABLE IF NOT EXISTS upload_batches (
    source_message_id INTEGER PRIMARY KEY,
    source_channel_id INTEGER NOT NULL,
    summary_message_id INTEGER NOT NULL,
    summary_channel_id INTEGER NOT NULL,
    principal_id INTEGER NOT NULL,
    total_items INTEGER NOT NULL,
    source_cleaned INTEGER NOT NULL DEFAULT 0,
    summary_cleaned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upload_items (
    source_message_id INTEGER NOT NULL REFERENCES upload_batches(source_message_id)
        ON DELETE CASCADE,
    attachment_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    state TEXT NOT NULL,
    job_id TEXT,
    document_id INTEGER,
    parent_message_id INTEGER,
    parent_channel_id INTEGER,
    thread_id INTEGER,
    title_message_id INTEGER,
    metadata_message_id INTEGER,
    actions_message_id INTEGER,
    controls_message_id INTEGER,
    review_finalization_state TEXT NOT NULL DEFAULT 'not_started',
    parent_cleaned INTEGER NOT NULL DEFAULT 0,
    thread_cleaned INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_message_id, attachment_id),
    UNIQUE (job_id),
    UNIQUE (source_message_id, ordinal)
);

CREATE INDEX IF NOT EXISTS upload_items_state_idx ON upload_items(state);
CREATE INDEX IF NOT EXISTS upload_items_document_idx ON upload_items(document_id);

CREATE TABLE IF NOT EXISTS reference_context (
    principal_id INTEGER PRIMARY KEY,
    document_ids_json TEXT NOT NULL,
    source_message_ids_json TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS question_messages (
    discord_message_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS question_messages_created_idx ON question_messages(created_at);

CREATE TABLE IF NOT EXISTS warning_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    discord_message_id INTEGER NOT NULL,
    emitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    job_id TEXT,
    task_id TEXT,
    document_id INTEGER,
    delivery_method TEXT
);

CREATE INDEX IF NOT EXISTS audit_events_occurred_idx ON audit_events(occurred_at);
"""


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _fernet_from_secret(secret: SecretStr) -> Fernet:
    return Fernet(secret.get_secret_value().encode("ascii"))


class SQLiteRepository:
    """One SQLite implementation for persistence ports."""

    def __init__(
        self,
        database_path: Path,
        *,
        lease_seconds: int,
        encryption_key: SecretStr | None = None,
    ) -> None:
        self._database_path = database_path
        self._lease = timedelta(seconds=lease_seconds)
        self._fernet = _fernet_from_secret(encryption_key) if encryption_key else None

    async def save_user_token(self, principal_id: int, token: SecretStr) -> None:
        """Encrypt and persist a Discord user's Paperless API token."""
        if self._fernet is None:
            raise ValueError("encryption key is not configured")
        now = _iso(_utc_now())
        encrypted = self._fernet.encrypt(token.get_secret_value().encode("utf-8"))
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO user_credentials(principal_id, encrypted_token, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(principal_id) DO UPDATE SET
                    encrypted_token=excluded.encrypted_token,
                    updated_at=excluded.updated_at
                """,
                (principal_id, encrypted, now, now),
            )
            connection.commit()

    async def get_user_token(self, principal_id: int) -> SecretStr | None:
        """Read and decrypt one user's Paperless API token."""
        if self._fernet is None:
            raise ValueError("encryption key is not configured")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT encrypted_token FROM user_credentials WHERE principal_id = ?",
                (principal_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            decrypted = self._fernet.decrypt(row["encrypted_token"])
            return SecretStr(decrypted.decode("utf-8"))
        except Exception:
            return None

    async def delete_user_token(self, principal_id: int) -> bool:
        """Revoke and delete a mapped user credential."""
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM user_credentials WHERE principal_id = ?", (principal_id,)
            )
            connection.commit()
            return cursor.rowcount == 1

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            yield connection
        finally:
            connection.close()

    async def initialize(self) -> None:
        """Create restricted directories and migrate the schema forward."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._database_path.parent.chmod(0o700)
        with self._connection() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(ingestion_jobs)").fetchall()
            }
            if "discord_status_message_id" not in columns:
                connection.execute(
                    "ALTER TABLE ingestion_jobs ADD COLUMN discord_status_message_id INTEGER"
                )
            for name, definition in (
                ("discord_message_channel_id", "INTEGER"),
                ("discord_status_channel_id", "INTEGER"),
                ("discord_message_cleaned", "INTEGER NOT NULL DEFAULT 0"),
                ("discord_status_message_cleaned", "INTEGER NOT NULL DEFAULT 0"),
                ("duplicate_confirmed", "INTEGER NOT NULL DEFAULT 0"),
                (
                    "review_finalization_state",
                    "TEXT NOT NULL DEFAULT 'not_started'",
                ),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE ingestion_jobs ADD COLUMN {name} {definition}")
            question_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(question_messages)").fetchall()
            }
            if "channel_id" not in question_columns:
                connection.execute("ALTER TABLE question_messages ADD COLUMN channel_id INTEGER")
            upload_item_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(upload_items)").fetchall()
            }
            for name, definition in (
                ("title_message_id", "INTEGER"),
                ("metadata_message_id", "INTEGER"),
                ("actions_message_id", "INTEGER"),
                ("controls_message_id", "INTEGER"),
                (
                    "review_finalization_state",
                    "TEXT NOT NULL DEFAULT 'not_started'",
                ),
                ("parent_cleaned", "INTEGER NOT NULL DEFAULT 0"),
                ("thread_cleaned", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in upload_item_columns:
                    connection.execute(f"ALTER TABLE upload_items ADD COLUMN {name} {definition}")
            connection.commit()
        self._database_path.chmod(0o600)

    async def close(self) -> None:
        """Connections are operation-scoped, so no shared handle remains."""

    async def acquire_instance(self, instance_id: UUID) -> bool:
        """Acquire or refresh the single-worker lease atomically."""
        now = _utc_now()
        stale_before = now - self._lease
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "SELECT instance_id, heartbeat_at FROM instance_lease WHERE singleton = 1"
            )
            row = cursor.fetchone()
            if (
                row is not None
                and row["instance_id"] != str(instance_id)
                and _parse_datetime(row["heartbeat_at"]) >= stale_before
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO instance_lease(singleton, instance_id, heartbeat_at)
                VALUES(1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    instance_id=excluded.instance_id,
                    heartbeat_at=excluded.heartbeat_at
                """,
                (str(instance_id), _iso(now)),
            )
            connection.commit()
            return True

    async def release_instance(self, instance_id: UUID) -> None:
        """Release only the caller's lease."""
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM instance_lease WHERE singleton = 1 AND instance_id = ?",
                (str(instance_id),),
            )
            connection.commit()

    async def create_job(self, job: IngestionJob) -> bool:
        """Create an idempotent job for one Discord attachment event."""
        now = job.created_at or _utc_now()
        guidance = {
            "tag_ids": list(job.guidance.tag_ids),
            "correspondent_id": job.guidance.correspondent_id,
            "document_type_id": job.guidance.document_type_id,
        }
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO ingestion_jobs(
                    id, discord_message_id, discord_attachment_id,
                    discord_status_message_id, discord_message_channel_id,
                    discord_status_channel_id, principal_id, staged_path,
                    original_filename, media_type, office_dependent, caption,
                    guidance_json, state, paperless_task_id,
                    paperless_document_id, duplicate_confirmed, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job.id),
                    job.discord_message_id,
                    job.discord_attachment_id,
                    job.discord_status_message_id,
                    job.discord_message_channel_id,
                    job.discord_status_channel_id,
                    job.principal_id,
                    str(job.staged_path),
                    job.original_filename,
                    job.media_type,
                    int(job.office_dependent),
                    job.caption,
                    json.dumps(guidance, separators=(",", ":")),
                    job.state.value,
                    str(job.paperless_task_id) if job.paperless_task_id else None,
                    int(job.paperless_document_id) if job.paperless_document_id else None,
                    int(job.duplicate_confirmed),
                    _iso(now),
                    _iso(job.updated_at or now),
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> IngestionJob:
        guidance: dict[str, Any] = json.loads(row["guidance_json"])
        return IngestionJob(
            id=UUID(row["id"]),
            discord_message_id=row["discord_message_id"],
            discord_attachment_id=row["discord_attachment_id"],
            discord_status_message_id=row["discord_status_message_id"],
            discord_message_channel_id=row["discord_message_channel_id"],
            discord_status_channel_id=row["discord_status_channel_id"],
            principal_id=row["principal_id"],
            staged_path=Path(row["staged_path"]),
            original_filename=row["original_filename"],
            media_type=row["media_type"],
            office_dependent=bool(row["office_dependent"]),
            caption=row["caption"],
            guidance=MetadataGuidance(
                tag_ids=tuple(guidance["tag_ids"]),
                correspondent_id=guidance["correspondent_id"],
                document_type_id=guidance["document_type_id"],
            ),
            state=JobState(row["state"]),
            paperless_task_id=UUID(row["paperless_task_id"]) if row["paperless_task_id"] else None,
            paperless_document_id=DocumentId(row["paperless_document_id"])
            if row["paperless_document_id"] is not None
            else None,
            duplicate_confirmed=bool(row["duplicate_confirmed"]),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    async def get_job(self, job_id: UUID) -> IngestionJob | None:
        """Read one job by its opaque application UUID."""
        with self._connection() as connection:
            cursor = connection.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (str(job_id),))
            row = cursor.fetchone()
        return self._job_from_row(row) if row is not None else None

    async def create_upload_batch(self, batch: UploadBatch, items: tuple[UploadItem, ...]) -> None:
        """Persist the complete attachment list before ingestion begins."""
        if len(items) != batch.total_items:
            raise ValueError("upload batch item count does not match total_items")
        if {item.ordinal for item in items} != set(range(1, batch.total_items + 1)):
            raise ValueError("upload item ordinals must be contiguous and one-based")
        now = batch.created_at or _utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO upload_batches(
                    source_message_id, source_channel_id, summary_message_id,
                    summary_channel_id, principal_id, total_items,
                    source_cleaned, summary_cleaned, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.source_message_id,
                    batch.source_channel_id,
                    batch.summary_message_id,
                    batch.summary_channel_id,
                    batch.principal_id,
                    batch.total_items,
                    int(batch.source_cleaned),
                    int(batch.summary_cleaned),
                    _iso(now),
                    _iso(batch.updated_at or now),
                ),
            )
            connection.executemany(
                """
                INSERT OR IGNORE INTO upload_items(
                    source_message_id, attachment_id, ordinal, original_filename,
                    state, job_id, document_id, parent_message_id, parent_channel_id,
                    thread_id, title_message_id, metadata_message_id, actions_message_id,
                    controls_message_id, review_finalization_state, parent_cleaned,
                    thread_cleaned, failure_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        item.source_message_id,
                        item.attachment_id,
                        item.ordinal,
                        item.original_filename,
                        item.state.value,
                        str(item.job_id) if item.job_id else None,
                        int(item.document_id) if item.document_id else None,
                        item.parent_message_id,
                        item.parent_channel_id,
                        item.thread_id,
                        item.title_message_id,
                        item.metadata_message_id,
                        item.actions_message_id,
                        item.controls_message_id,
                        item.review_finalization_state.value,
                        int(item.parent_cleaned),
                        int(item.thread_cleaned),
                        item.failure_reason,
                        _iso(item.created_at or now),
                        _iso(item.updated_at or now),
                    )
                    for item in items
                ),
            )
            connection.commit()

    @staticmethod
    def _upload_batch_from_row(row: sqlite3.Row) -> UploadBatch:
        return UploadBatch(
            source_message_id=row["source_message_id"],
            source_channel_id=row["source_channel_id"],
            summary_message_id=row["summary_message_id"],
            summary_channel_id=row["summary_channel_id"],
            principal_id=row["principal_id"],
            total_items=row["total_items"],
            source_cleaned=bool(row["source_cleaned"]),
            summary_cleaned=bool(row["summary_cleaned"]),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _upload_item_from_row(row: sqlite3.Row) -> UploadItem:
        return UploadItem(
            source_message_id=row["source_message_id"],
            attachment_id=row["attachment_id"],
            ordinal=row["ordinal"],
            original_filename=row["original_filename"],
            state=UploadItemState(row["state"]),
            job_id=UUID(row["job_id"]) if row["job_id"] else None,
            document_id=DocumentId(row["document_id"]) if row["document_id"] is not None else None,
            parent_message_id=row["parent_message_id"],
            parent_channel_id=row["parent_channel_id"],
            thread_id=row["thread_id"],
            title_message_id=row["title_message_id"],
            metadata_message_id=row["metadata_message_id"],
            actions_message_id=row["actions_message_id"],
            controls_message_id=row["controls_message_id"],
            review_finalization_state=ReviewFinalizationState(row["review_finalization_state"]),
            parent_cleaned=bool(row["parent_cleaned"]),
            thread_cleaned=bool(row["thread_cleaned"]),
            failure_reason=row["failure_reason"],
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    async def get_upload_batch(self, source_message_id: int) -> UploadBatchSnapshot | None:
        """Load one durable batch and its items in original attachment order."""
        with self._connection() as connection:
            batch_row = connection.execute(
                "SELECT * FROM upload_batches WHERE source_message_id = ?",
                (source_message_id,),
            ).fetchone()
            if batch_row is None:
                return None
            item_rows = connection.execute(
                """
                SELECT * FROM upload_items
                WHERE source_message_id = ?
                ORDER BY ordinal
                """,
                (source_message_id,),
            ).fetchall()
        return UploadBatchSnapshot(
            self._upload_batch_from_row(batch_row),
            tuple(self._upload_item_from_row(row) for row in item_rows),
        )

    async def update_upload_item(  # noqa: PLR0913
        self,
        source_message_id: int,
        attachment_id: int,
        state: UploadItemState,
        *,
        job_id: UUID | None = None,
        document_id: int | None = None,
        parent_message_id: int | None = None,
        parent_channel_id: int | None = None,
        thread_id: int | None = None,
        title_message_id: int | None = None,
        metadata_message_id: int | None = None,
        actions_message_id: int | None = None,
        controls_message_id: int | None = None,
        failure_reason: str | None = None,
    ) -> UploadBatchSnapshot:
        """Update one item and return the batch snapshot used for cleanup decisions."""
        now = _iso(_utc_now())
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE upload_items
                SET state = ?,
                    job_id = COALESCE(?, job_id),
                    document_id = COALESCE(?, document_id),
                    parent_message_id = COALESCE(?, parent_message_id),
                    parent_channel_id = COALESCE(?, parent_channel_id),
                    thread_id = COALESCE(?, thread_id),
                    title_message_id = COALESCE(?, title_message_id),
                    metadata_message_id = COALESCE(?, metadata_message_id),
                    actions_message_id = COALESCE(?, actions_message_id),
                    controls_message_id = COALESCE(?, controls_message_id),
                    failure_reason = COALESCE(?, failure_reason),
                    updated_at = ?
                WHERE source_message_id = ? AND attachment_id = ?
                """,
                (
                    state.value,
                    str(job_id) if job_id else None,
                    document_id,
                    parent_message_id,
                    parent_channel_id,
                    thread_id,
                    title_message_id,
                    metadata_message_id,
                    actions_message_id,
                    controls_message_id,
                    failure_reason,
                    now,
                    source_message_id,
                    attachment_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("upload item does not exist")
            connection.execute(
                "UPDATE upload_batches SET updated_at = ? WHERE source_message_id = ?",
                (now, source_message_id),
            )
            connection.commit()
        snapshot = await self.get_upload_batch(source_message_id)
        if snapshot is None:
            raise ValueError("upload batch disappeared")
        return snapshot

    async def upload_item_for_job(self, job_id: UUID) -> UploadItem | None:
        """Load the batch item linked to one ingestion job."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM upload_items WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
        return self._upload_item_from_row(row) if row is not None else None

    async def set_review_finalization_state(
        self,
        job_id: UUID,
        state: ReviewFinalizationState,
    ) -> bool:
        """Persist the save/notification cleanup gate for one durable review."""
        now = _iso(_utc_now())
        with self._connection() as connection:
            item_cursor = connection.execute(
                """
                UPDATE upload_items
                SET review_finalization_state = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (state.value, now, str(job_id)),
            )
            job_cursor = connection.execute(
                """
                UPDATE ingestion_jobs
                SET review_finalization_state = ?, updated_at = ?
                WHERE id = ?
                """,
                (state.value, now, str(job_id)),
            )
            if item_cursor.rowcount:
                connection.execute(
                    """
                    UPDATE upload_batches
                    SET updated_at = ?
                    WHERE source_message_id = (
                        SELECT source_message_id FROM upload_items WHERE job_id = ?
                    )
                    """,
                    (now, str(job_id)),
                )
            connection.commit()
        return item_cursor.rowcount == 1 or job_cursor.rowcount == 1

    async def close_upload_item_if_cleanup_eligible(
        self,
        source_message_id: int,
        attachment_id: int,
    ) -> UploadBatchSnapshot | None:
        """Atomically close an inbox-removed item only while its save gate permits."""
        now = _iso(_utc_now())
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE upload_items
                SET state = ?, updated_at = ?
                WHERE source_message_id = ?
                  AND attachment_id = ?
                  AND state = ?
                  AND review_finalization_state IN (?, ?)
                """,
                (
                    UploadItemState.CLOSED.value,
                    now,
                    source_message_id,
                    attachment_id,
                    UploadItemState.SUCCEEDED.value,
                    ReviewFinalizationState.NOT_STARTED.value,
                    ReviewFinalizationState.READY_FOR_CLEANUP.value,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE upload_batches SET updated_at = ? WHERE source_message_id = ?",
                (now, source_message_id),
            )
            connection.commit()
        snapshot = await self.get_upload_batch(source_message_id)
        if snapshot is None:
            raise ValueError("upload batch disappeared")
        return snapshot

    async def active_upload_items(self) -> tuple[UploadItem, ...]:
        """Return non-resolved new-model items for recovery and inbox cleanup."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM upload_items
                WHERE state NOT IN (?, ?)
                ORDER BY created_at, source_message_id, ordinal
                """,
                (UploadItemState.CLOSED.value, UploadItemState.DISMISSED.value),
            ).fetchall()
        return tuple(self._upload_item_from_row(row) for row in rows)

    async def tracked_upload_items(self) -> tuple[UploadItem, ...]:
        """Return every durable per-file Discord artifact for reconciliation."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM upload_items
                ORDER BY created_at, source_message_id, ordinal
                """
            ).fetchall()
        return tuple(self._upload_item_from_row(row) for row in rows)

    async def resolved_upload_items_pending_cleanup(self) -> tuple[UploadItem, ...]:
        """Return resolved per-file artifacts whose Discord deletion is unconfirmed."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM upload_items
                WHERE state IN (?, ?)
                  AND (
                    (parent_message_id IS NOT NULL AND parent_cleaned = 0)
                    OR (thread_id IS NOT NULL AND thread_cleaned = 0)
                  )
                ORDER BY created_at, source_message_id, ordinal
                """,
                (UploadItemState.CLOSED.value, UploadItemState.DISMISSED.value),
            ).fetchall()
        return tuple(self._upload_item_from_row(row) for row in rows)

    async def confirm_upload_item_cleanup(
        self,
        source_message_id: int,
        attachment_id: int,
        *,
        parent_cleaned: bool,
        thread_cleaned: bool,
    ) -> None:
        """Record only per-file Discord artifacts confirmed deleted or absent."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE upload_items
                SET parent_cleaned = CASE WHEN ? THEN 1 ELSE parent_cleaned END,
                    thread_cleaned = CASE WHEN ? THEN 1 ELSE thread_cleaned END,
                    updated_at = ?
                WHERE source_message_id = ? AND attachment_id = ?
                """,
                (
                    int(parent_cleaned),
                    int(thread_cleaned),
                    _iso(_utc_now()),
                    source_message_id,
                    attachment_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("upload item does not exist")
            connection.commit()

    async def terminal_upload_cleanup_targets(
        self,
    ) -> tuple[DiscordMessageTarget, ...]:
        """Return shared artifacts only for batches with no uncertain or active item."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    b.source_message_id,
                    b.source_channel_id,
                    b.summary_message_id,
                    b.summary_channel_id,
                    b.source_cleaned,
                    b.summary_cleaned,
                    MAX(CASE WHEN i.state NOT IN (?, ?) THEN 1 ELSE 0 END) AS unresolved
                FROM upload_batches AS b
                JOIN upload_items AS i
                  ON i.source_message_id = b.source_message_id
                GROUP BY b.source_message_id
                HAVING unresolved = 0
                ORDER BY b.created_at, b.source_message_id
                """,
                (
                    UploadItemState.CLOSED.value,
                    UploadItemState.DISMISSED.value,
                ),
            ).fetchall()
        targets: list[DiscordMessageTarget] = []
        for row in rows:
            if not row["summary_cleaned"]:
                targets.append(
                    DiscordMessageTarget(
                        row["summary_channel_id"],
                        row["summary_message_id"],
                    )
                )
            if not row["source_cleaned"]:
                targets.append(
                    DiscordMessageTarget(
                        row["source_channel_id"],
                        row["source_message_id"],
                    )
                )
        return tuple(targets)

    async def transition_job(  # noqa: PLR0913
        self,
        job_id: UUID,
        expected: JobState,
        target: JobState,
        *,
        task_id: UUID | None = None,
        document_id: int | None = None,
        duplicate_confirmed: bool = False,
    ) -> bool:
        """Apply a compare-and-swap state transition."""
        if target not in ALLOWED_JOB_TRANSITIONS[expected]:
            raise ValueError(f"invalid job transition: {expected.value} -> {target.value}")
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_jobs
                SET state = ?,
                    paperless_task_id = COALESCE(?, paperless_task_id),
                    paperless_document_id = COALESCE(?, paperless_document_id),
                    duplicate_confirmed =
                        CASE WHEN ? THEN 1 ELSE duplicate_confirmed END,
                    updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    target.value,
                    str(task_id) if task_id else None,
                    document_id,
                    int(duplicate_confirmed),
                    _iso(_utc_now()),
                    str(job_id),
                    expected.value,
                ),
            )
            if cursor.rowcount == 1:
                upload_state = {
                    JobState.STAGED: UploadItemState.PENDING,
                    JobState.SUBMITTING: UploadItemState.PROCESSING,
                    JobState.SUBMITTED: UploadItemState.PROCESSING,
                    JobState.SUCCEEDED: UploadItemState.SUCCEEDED,
                    JobState.FAILED: UploadItemState.FAILED,
                    JobState.RECONCILIATION_REQUIRED: (UploadItemState.RECONCILIATION_REQUIRED),
                }[target]
                connection.execute(
                    """
                    UPDATE upload_items
                    SET state = ?,
                        document_id = COALESCE(?, document_id),
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        upload_state.value,
                        document_id,
                        _iso(_utc_now()),
                        str(job_id),
                    ),
                )
            connection.commit()
            return cursor.rowcount == 1

    async def recoverable_jobs(self) -> tuple[IngestionJob, ...]:
        """Return jobs safe to resume without repeating an ambiguous POST."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                SELECT * FROM ingestion_jobs
                WHERE state IN (?, ?, ?)
                ORDER BY created_at, discord_attachment_id
                """,
                (
                    JobState.STAGED.value,
                    JobState.SUBMITTING.value,
                    JobState.SUBMITTED.value,
                ),
            )
            rows = cursor.fetchall()
        return tuple(self._job_from_row(row) for row in rows)

    async def save_context(self, context: ReferenceContext) -> None:
        """Replace one user's non-sensitive ordered reference context."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO reference_context(
                    principal_id, document_ids_json, source_message_ids_json, expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(principal_id) DO UPDATE SET
                    document_ids_json=excluded.document_ids_json,
                    source_message_ids_json=excluded.source_message_ids_json,
                    expires_at=excluded.expires_at
                """,
                (
                    context.principal_id,
                    json.dumps([int(item) for item in context.document_ids]),
                    json.dumps(list(context.source_message_ids)),
                    _iso(context.expires_at),
                ),
            )
            created_at = _iso(_utc_now())
            connection.executemany(
                """
                INSERT INTO question_messages(discord_message_id, channel_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_message_id) DO UPDATE SET
                    channel_id=excluded.channel_id
                """,
                (
                    (message_id, context.principal_id, created_at)
                    for message_id in context.source_message_ids
                ),
            )
            connection.commit()

    async def get_context(self, principal_id: int) -> ReferenceContext | None:
        """Return active context while retaining cleanup IDs after expiry."""
        with self._connection() as connection:
            cursor = connection.execute(
                "SELECT * FROM reference_context WHERE principal_id = ?", (principal_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            expires_at = _parse_datetime(row["expires_at"])
            if expires_at <= _utc_now():
                return None
        return ReferenceContext(
            principal_id=principal_id,
            document_ids=tuple(DocumentId(value) for value in json.loads(row["document_ids_json"])),
            source_message_ids=tuple(json.loads(row["source_message_ids_json"])),
            expires_at=expires_at,
        )

    async def record(self, event: AuditEvent) -> None:
        """Persist only explicitly allowlisted audit fields."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(
                    principal_id, action, outcome, occurred_at, correlation_id,
                    job_id, task_id, document_id, delivery_method
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.principal_id,
                    event.action,
                    event.outcome,
                    _iso(event.occurred_at),
                    str(event.correlation_id),
                    str(event.job_id) if event.job_id else None,
                    str(event.task_id) if event.task_id else None,
                    int(event.document_id) if event.document_id else None,
                    event.delivery_method,
                ),
            )
            connection.commit()

    async def cleanup_message_ids(
        self, *, context_before: str, succeeded_before: str, failed_before: str
    ) -> tuple[tuple[DiscordMessageTarget, ...], tuple[DiscordMessageTarget, ...]]:
        """Return exact old question and resolved-upload Discord targets."""
        with self._connection() as connection:
            context_cursor = connection.execute(
                """
                SELECT channel_id, discord_message_id FROM question_messages
                WHERE created_at < ?
                  AND channel_id IS NOT NULL
                ORDER BY created_at, channel_id, discord_message_id
                """,
                (context_before,),
            )
            context_targets = tuple(
                DiscordMessageTarget(row["channel_id"], row["discord_message_id"])
                for row in context_cursor.fetchall()
            )
            upload_cursor = connection.execute(
                """
                SELECT
                    discord_message_id,
                    discord_status_message_id,
                    discord_message_channel_id,
                    discord_status_channel_id,
                    discord_message_cleaned,
                    discord_status_message_cleaned,
                    MAX(CASE WHEN state NOT IN (?, ?) THEN 1 ELSE 0 END) AS unresolved,
                    MAX(CASE WHEN state = ? THEN 1 ELSE 0 END) AS has_failed,
                    MAX(
                        CASE WHEN review_finalization_state IN (?, ?) THEN 1 ELSE 0 END
                    ) AS finalization_blocked,
                    MAX(updated_at) AS newest_update
                FROM ingestion_jobs
                WHERE NOT EXISTS (
                    SELECT 1 FROM upload_items
                    WHERE upload_items.job_id = ingestion_jobs.id
                )
                GROUP BY
                    discord_message_id,
                    discord_status_message_id,
                    discord_message_channel_id,
                    discord_status_channel_id,
                    discord_message_cleaned,
                    discord_status_message_cleaned
                """,
                (
                    JobState.SUCCEEDED.value,
                    JobState.FAILED.value,
                    JobState.FAILED.value,
                    ReviewFinalizationState.PENDING_NOTIFICATION.value,
                    ReviewFinalizationState.NEEDS_RECONCILIATION.value,
                ),
            )
            upload_targets: list[DiscordMessageTarget] = []
            for row in upload_cursor.fetchall():
                eligible = (
                    row["unresolved"] == 0
                    and row["finalization_blocked"] == 0
                    and (
                        (row["has_failed"] == 0 and row["newest_update"] < succeeded_before)
                        or (row["has_failed"] == 1 and row["newest_update"] < failed_before)
                    )
                )
                if not eligible:
                    continue
                if (
                    not row["discord_message_cleaned"]
                    and row["discord_message_channel_id"] is not None
                ):
                    upload_targets.append(
                        DiscordMessageTarget(
                            row["discord_message_channel_id"],
                            row["discord_message_id"],
                        )
                    )
                if (
                    not row["discord_status_message_cleaned"]
                    and row["discord_status_channel_id"] is not None
                    and row["discord_status_message_id"] is not None
                ):
                    upload_targets.append(
                        DiscordMessageTarget(
                            row["discord_status_channel_id"],
                            row["discord_status_message_id"],
                        )
                    )
            batch_cursor = connection.execute(
                """
                SELECT
                    b.source_message_id,
                    b.source_channel_id,
                    b.summary_message_id,
                    b.summary_channel_id,
                    b.source_cleaned,
                    b.summary_cleaned,
                    MAX(CASE WHEN i.state NOT IN (?, ?) THEN 1 ELSE 0 END) AS unresolved,
                    MAX(CASE WHEN i.state = ? THEN 1 ELSE 0 END) AS has_failed,
                    MAX(i.updated_at) AS newest_update
                FROM upload_batches AS b
                JOIN upload_items AS i
                  ON i.source_message_id = b.source_message_id
                GROUP BY b.source_message_id
                """,
                (
                    UploadItemState.CLOSED.value,
                    UploadItemState.DISMISSED.value,
                    UploadItemState.DISMISSED.value,
                ),
            )
            for row in batch_cursor.fetchall():
                eligible = row["unresolved"] == 0 and (
                    (row["has_failed"] == 0 and row["newest_update"] < succeeded_before)
                    or (row["has_failed"] == 1 and row["newest_update"] < failed_before)
                )
                if not eligible:
                    continue
                if not row["summary_cleaned"]:
                    upload_targets.append(
                        DiscordMessageTarget(
                            row["summary_channel_id"],
                            row["summary_message_id"],
                        )
                    )
                if not row["source_cleaned"]:
                    upload_targets.append(
                        DiscordMessageTarget(
                            row["source_channel_id"],
                            row["source_message_id"],
                        )
                    )
        return context_targets, tuple(dict.fromkeys(upload_targets))

    async def confirm_message_cleanup(self, targets: tuple[DiscordMessageTarget, ...]) -> None:
        """Forget cleanup evidence only after Discord confirms deletion or absence."""
        with self._connection() as connection:
            for target in targets:
                connection.execute(
                    """
                    DELETE FROM question_messages
                    WHERE channel_id = ? AND discord_message_id = ?
                    """,
                    (target.channel_id, target.message_id),
                )
                connection.execute(
                    """
                    UPDATE ingestion_jobs
                    SET discord_message_cleaned = 1
                    WHERE discord_message_channel_id = ? AND discord_message_id = ?
                    """,
                    (target.channel_id, target.message_id),
                )
                connection.execute(
                    """
                    UPDATE ingestion_jobs
                    SET discord_status_message_cleaned = 1
                    WHERE discord_status_channel_id = ? AND discord_status_message_id = ?
                    """,
                    (target.channel_id, target.message_id),
                )
                connection.execute(
                    """
                    UPDATE upload_batches
                    SET source_cleaned = 1, updated_at = ?
                    WHERE source_channel_id = ? AND source_message_id = ?
                    """,
                    (_iso(_utc_now()), target.channel_id, target.message_id),
                )
                connection.execute(
                    """
                    UPDATE upload_batches
                    SET summary_cleaned = 1, updated_at = ?
                    WHERE summary_channel_id = ? AND summary_message_id = ?
                    """,
                    (_iso(_utc_now()), target.channel_id, target.message_id),
                )
            connection.commit()

    async def message_job_states(self, discord_message_id: int) -> tuple[JobState, ...]:
        """Return durable attachment states for recovery-time source cleanup."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                SELECT state FROM ingestion_jobs
                WHERE discord_message_id = ?
                ORDER BY discord_attachment_id
                """,
                (discord_message_id,),
            )
            rows = cursor.fetchall()
        return tuple(JobState(row["state"]) for row in rows)

    async def active_succeeded_uploads(
        self,
    ) -> tuple[tuple[tuple[DiscordMessageTarget, ...], int], ...]:
        """Return exact active Discord targets grouped by succeeded document."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                SELECT
                    discord_status_message_id,
                    discord_message_id,
                    discord_message_channel_id,
                    discord_status_channel_id,
                    discord_message_cleaned,
                    discord_status_message_cleaned,
                    paperless_document_id
                FROM ingestion_jobs
                WHERE state = ?
                  AND paperless_document_id IS NOT NULL
                  AND review_finalization_state IN (?, ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM upload_items
                      WHERE upload_items.job_id = ingestion_jobs.id
                  )
                  AND (
                    discord_message_cleaned = 0
                    OR (
                        discord_status_message_id IS NOT NULL
                        AND discord_status_message_cleaned = 0
                    )
                  )
                """,
                (
                    JobState.SUCCEEDED.value,
                    ReviewFinalizationState.NOT_STARTED.value,
                    ReviewFinalizationState.READY_FOR_CLEANUP.value,
                ),
            )
            rows = cursor.fetchall()
        results: list[tuple[tuple[DiscordMessageTarget, ...], int]] = []
        for row in rows:
            targets: list[DiscordMessageTarget] = []
            if not row["discord_message_cleaned"] and row["discord_message_channel_id"] is not None:
                targets.append(
                    DiscordMessageTarget(
                        row["discord_message_channel_id"],
                        row["discord_message_id"],
                    )
                )
            if (
                not row["discord_status_message_cleaned"]
                and row["discord_status_channel_id"] is not None
                and row["discord_status_message_id"] is not None
            ):
                targets.append(
                    DiscordMessageTarget(
                        row["discord_status_channel_id"],
                        row["discord_status_message_id"],
                    )
                )
            results.append((tuple(dict.fromkeys(targets)), int(row["paperless_document_id"])))
        return tuple(results)

    async def protected_staged_paths(self) -> frozenset[Path]:
        """Return every job-owned path, including reconciliation evidence."""
        with self._connection() as connection:
            rows = connection.execute("SELECT staged_path FROM ingestion_jobs").fetchall()
        return frozenset(Path(row["staged_path"]) for row in rows)

    async def get_warning_state(self) -> tuple[int, datetime] | None:
        """Return the persisted missing-tag warning identity and timestamp."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT discord_message_id, emitted_at FROM warning_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return row["discord_message_id"], _parse_datetime(row["emitted_at"])

    async def save_warning_state(self, message_id: int, emitted_at: datetime) -> None:
        """Persist only the message ID and timestamp needed to bound warnings."""
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO warning_state(singleton, discord_message_id, emitted_at)
                VALUES(1, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    discord_message_id=excluded.discord_message_id,
                    emitted_at=excluded.emitted_at
                """,
                (message_id, _iso(emitted_at)),
            )
            connection.commit()

    async def clear_warning_state(self) -> None:
        """Forget the resolved warning after Discord cleanup."""
        with self._connection() as connection:
            connection.execute("DELETE FROM warning_state WHERE singleton = 1")
            connection.commit()

    def _action_names(self) -> tuple[str, ...]:
        with self._connection() as connection:
            cursor = connection.execute("SELECT action FROM audit_events ORDER BY id")
            rows = cursor.fetchall()
        return tuple(row["action"] for row in rows)

    async def actions(self) -> AsyncIterator[str]:
        """Yield action names for privacy-focused contract tests."""
        for action in self._action_names():
            yield action

    async def purge(
        self,
        *,
        expired_before: str,
        audit_before: str,
        succeeded_before: str,
        failed_before: str,
    ) -> None:
        """Purge expired state only after Discord cleanup evidence is confirmed."""
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM reference_context WHERE expires_at < ?", (expired_before,)
            )
            connection.execute("DELETE FROM audit_events WHERE occurred_at < ?", (audit_before,))
            connection.execute(
                """
                DELETE FROM ingestion_jobs
                WHERE discord_message_cleaned = 1
                  AND (
                    discord_status_message_id IS NULL
                    OR discord_status_message_cleaned = 1
                  )
                  AND (
                    (state = ? AND updated_at < ?)
                    OR (state = ? AND updated_at < ?)
                  )
                """,
                (
                    JobState.SUCCEEDED.value,
                    succeeded_before,
                    JobState.FAILED.value,
                    failed_before,
                ),
            )
            connection.execute(
                """
                DELETE FROM upload_batches
                WHERE source_cleaned = 1
                  AND summary_cleaned = 1
                  AND updated_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM upload_items
                      WHERE upload_items.source_message_id =
                            upload_batches.source_message_id
                        AND state IN (?, ?, ?)
                  )
                """,
                (
                    failed_before,
                    UploadItemState.PENDING.value,
                    UploadItemState.PROCESSING.value,
                    UploadItemState.RECONCILIATION_REQUIRED.value,
                ),
            )
            connection.commit()
