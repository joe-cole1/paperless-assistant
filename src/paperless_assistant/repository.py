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

from paperless_assistant.models import (
    ALLOWED_JOB_TRANSITIONS,
    AuditEvent,
    DocumentId,
    IngestionJob,
    JobState,
    MetadataGuidance,
    ReferenceContext,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (discord_message_id, discord_attachment_id)
);

CREATE INDEX IF NOT EXISTS ingestion_jobs_state_idx ON ingestion_jobs(state);
CREATE INDEX IF NOT EXISTS ingestion_jobs_updated_idx ON ingestion_jobs(updated_at);

CREATE TABLE IF NOT EXISTS reference_context (
    principal_id INTEGER PRIMARY KEY,
    document_ids_json TEXT NOT NULL,
    source_message_ids_json TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS question_messages (
    discord_message_id INTEGER PRIMARY KEY,
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


class SQLiteRepository:
    """One SQLite implementation for the issue #10 persistence ports."""

    def __init__(self, database_path: Path, *, lease_seconds: int) -> None:
        self._database_path = database_path
        self._lease = timedelta(seconds=lease_seconds)

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
                    discord_status_message_id, principal_id, staged_path,
                    original_filename, media_type, office_dependent, caption,
                    guidance_json, state, paperless_task_id,
                    paperless_document_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(job.id),
                    job.discord_message_id,
                    job.discord_attachment_id,
                    job.discord_status_message_id,
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
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    async def get_job(self, job_id: UUID) -> IngestionJob | None:
        """Read one job by its opaque application UUID."""
        with self._connection() as connection:
            cursor = connection.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (str(job_id),))
            row = cursor.fetchone()
        return self._job_from_row(row) if row is not None else None

    async def transition_job(
        self,
        job_id: UUID,
        expected: JobState,
        target: JobState,
        *,
        task_id: UUID | None = None,
        document_id: int | None = None,
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
                    updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    target.value,
                    str(task_id) if task_id else None,
                    document_id,
                    _iso(_utc_now()),
                    str(job_id),
                    expected.value,
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
                INSERT OR IGNORE INTO question_messages(discord_message_id, created_at)
                VALUES (?, ?)
                """,
                ((message_id, created_at) for message_id in context.source_message_ids),
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
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Return old question result IDs and resolved upload source IDs."""
        with self._connection() as connection:
            context_cursor = connection.execute(
                """
                SELECT discord_message_id FROM question_messages
                WHERE created_at < ?
                ORDER BY created_at, discord_message_id
                """,
                (context_before,),
            )
            context_ids = tuple(row["discord_message_id"] for row in context_cursor.fetchall())
            upload_cursor = connection.execute(
                """
                SELECT
                    discord_message_id,
                    discord_status_message_id,
                    MAX(CASE WHEN state NOT IN (?, ?) THEN 1 ELSE 0 END) AS unresolved,
                    MAX(CASE WHEN state = ? THEN 1 ELSE 0 END) AS has_failed,
                    MAX(updated_at) AS newest_update
                FROM ingestion_jobs
                GROUP BY discord_message_id, discord_status_message_id
                """,
                (
                    JobState.SUCCEEDED.value,
                    JobState.FAILED.value,
                    JobState.FAILED.value,
                ),
            )
            upload_ids = tuple(
                dict.fromkeys(
                    identifier
                    for row in upload_cursor.fetchall()
                    if row["unresolved"] == 0
                    and (
                        (row["has_failed"] == 0 and row["newest_update"] < succeeded_before)
                        or (row["has_failed"] == 1 and row["newest_update"] < failed_before)
                    )
                    for identifier in (
                        row["discord_message_id"],
                        row["discord_status_message_id"],
                    )
                    if identifier is not None
                )
            )
        return context_ids, upload_ids

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

    async def active_succeeded_uploads(self) -> tuple[tuple[int, int], ...]:
        """Return tuple of (discord_message_id, paperless_document_id) for active succeeded uploads."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                SELECT discord_status_message_id, discord_message_id, paperless_document_id
                FROM ingestion_jobs
                WHERE state = ? AND paperless_document_id IS NOT NULL
                """,
                (JobState.SUCCEEDED.value,),
            )
            rows = cursor.fetchall()
        results: list[tuple[int, int]] = []
        for row in rows:
            msg_id = row["discord_status_message_id"] or row["discord_message_id"]
            doc_id = row["paperless_document_id"]
            if msg_id and doc_id:
                results.append((int(msg_id), int(doc_id)))
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

    async def actions(self) -> AsyncIterator[str]:
        """Yield action names for privacy-focused contract tests."""
        with self._connection() as connection:
            cursor = connection.execute("SELECT action FROM audit_events ORDER BY id")
            rows = cursor.fetchall()
        for row in rows:
            yield row["action"]

    async def purge(self, *, context_before: str, audit_before: str, failed_before: str) -> None:
        """Purge expired context/audit and resolved transient job data."""
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM reference_context WHERE expires_at < ?", (context_before,)
            )
            connection.execute(
                "DELETE FROM question_messages WHERE created_at < ?", (context_before,)
            )
            connection.execute("DELETE FROM audit_events WHERE occurred_at < ?", (audit_before,))
            connection.execute(
                """
                DELETE FROM ingestion_jobs
                WHERE (state = ? AND updated_at < ?)
                   OR (state = ? AND updated_at < ?)
                """,
                (
                    JobState.SUCCEEDED.value,
                    context_before,
                    JobState.FAILED.value,
                    failed_before,
                ),
            )
            connection.commit()
