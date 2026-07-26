"""HTTP adapter for the narrow Paperless-ngx v3 API surface."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import httpx

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    AmbiguousSubmissionError,
    PaperlessAuthenticationError,
    PaperlessUnavailableError,
)
from paperless_assistant.models import (
    ChatResult,
    Document,
    DocumentId,
    Download,
    MetadataGuidance,
    PaperlessTask,
    TaskState,
    Taxonomy,
    TaxonomyItem,
)

CHAT_METADATA_DELIMITER = "\n\n__PAPERLESS_CHAT_METADATA__"
PAPERLESS_NO_CONTENT = "Sorry, I couldn't find any content to answer your question."
PAPERLESS_CHAT_ERROR = "Sorry, something went wrong while generating a response."


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _document(payload: object) -> Document:
    if not isinstance(payload, Mapping):
        raise PaperlessUnavailableError("malformed document response")
    identifier = payload.get("id")
    title = payload.get("title")
    if not isinstance(identifier, int) or not isinstance(title, str):
        raise PaperlessUnavailableError("malformed document response")
    original = payload.get("original_file_name")
    archived = payload.get("archived_file_name")
    return Document(
        id=DocumentId(identifier),
        title=title,
        created=_parse_date(payload.get("created")),
        original_filename=original if isinstance(original, str) else None,
        archived_filename=archived if isinstance(archived, str) else None,
    )


def parse_chat_response(response: str) -> ChatResult:
    """Parse Paperless's raw streamed text and final metadata trailer."""
    delimiter_index = response.find(CHAT_METADATA_DELIMITER)
    if delimiter_index == -1:
        return ChatResult(answer=response, document_ids=())
    answer = response[:delimiter_index]
    raw_metadata = response[delimiter_index + len(CHAT_METADATA_DELIMITER) :]
    try:
        metadata = json.loads(raw_metadata)
        references = metadata.get("references", [])
        identifiers = tuple(
            DocumentId(item["id"])
            for item in references[:3]
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        )
    except AttributeError, json.JSONDecodeError, TypeError:
        identifiers = ()
    return ChatResult(answer=answer, document_ids=identifiers)


def _filename_from_headers(headers: httpx.Headers, fallback: str) -> str:
    disposition = headers.get("content-disposition", "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    plain = re.search(r'filename="?([^";]+)"?', disposition, flags=re.IGNORECASE)
    candidate = unquote(encoded.group(1)) if encoded else plain.group(1) if plain else fallback
    return Path(candidate).name or fallback


class HttpPaperlessGateway:
    """Only component authorized to make Paperless HTTP requests."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        timeout = httpx.Timeout(
            connect=settings.paperless_connect_timeout_seconds,
            read=settings.paperless_read_timeout_seconds,
            write=settings.paperless_write_timeout_seconds,
            pool=settings.paperless_pool_timeout_seconds,
        )
        self._client = client or httpx.AsyncClient(
            base_url=str(settings.paperless_internal_url).rstrip("/") + "/",
            headers={
                "Authorization": f"Token {settings.paperless_token.get_secret_value()}",
                "Accept": f"application/json; version={settings.paperless_api_version}",
            },
            timeout=timeout,
            follow_redirects=False,
        )

    async def close(self) -> None:
        """Close the owned connection pool."""
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _raise_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise PaperlessAuthenticationError("Paperless authentication failed")
        if response.is_error:
            raise PaperlessUnavailableError(f"Paperless returned HTTP {response.status_code}")

    async def chat(self, question: str, document_id: int | None = None) -> ChatResult:
        """Complete one native Paperless streamed chat call."""
        try:
            async with self._client.stream(
                "POST",
                "api/documents/chat/",
                json={"q": question, "document_id": document_id},
                timeout=httpx.Timeout(self._settings.paperless_chat_timeout_seconds),
            ) as response:
                self._raise_status(response)
                chunks = [chunk async for chunk in response.aiter_text()]
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless chat unavailable") from error
        return parse_chat_response("".join(chunks))

    async def _paginated(self, endpoint: str) -> tuple[dict[str, Any], ...]:
        page = 1
        results: list[dict[str, Any]] = []
        while True:
            try:
                response = await self._client.get(endpoint, params={"page": page, "page_size": 100})
            except httpx.HTTPError as error:
                raise PaperlessUnavailableError("Paperless API unavailable") from error
            self._raise_status(response)
            try:
                payload = response.json()
                batch = payload["results"]
                next_page = payload.get("next")
            except (KeyError, TypeError, ValueError) as error:
                raise PaperlessUnavailableError("malformed paginated response") from error
            if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
                raise PaperlessUnavailableError("malformed paginated response")
            results.extend(batch)
            if not next_page:
                return tuple(results)
            page += 1

    async def search_documents(self, query: str, limit: int = 3) -> tuple[Document, ...]:
        """Use Paperless native full-text search without interpreting the query."""
        try:
            response = await self._client.get(
                "api/documents/",
                params={"query": query, "page_size": min(limit, 3)},
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless search unavailable") from error
        self._raise_status(response)
        try:
            payload = response.json()
            results = payload["results"]
        except (KeyError, TypeError, ValueError) as error:
            raise PaperlessUnavailableError("malformed search response") from error
        if not isinstance(results, list):
            raise PaperlessUnavailableError("malformed search response")
        return tuple(_document(item) for item in results[: min(limit, 3)])

    async def get_document(self, document_id: int) -> Document:
        """Fetch current visible metadata for a referenced document."""
        try:
            response = await self._client.get(f"api/documents/{document_id}/")
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless document unavailable") from error
        self._raise_status(response)
        try:
            return _document(response.json())
        except ValueError as error:
            raise PaperlessUnavailableError("malformed document response") from error

    async def get_taxonomy(self) -> Taxonomy:
        """Read every visible object in the three supported taxonomy categories."""
        tags = await self._paginated("api/tags/")
        correspondents = await self._paginated("api/correspondents/")
        document_types = await self._paginated("api/document_types/")

        def items(values: tuple[dict[str, Any], ...]) -> tuple[TaxonomyItem, ...]:
            resolved: list[TaxonomyItem] = []
            for value in values:
                identifier = value.get("id")
                name = value.get("name")
                if not isinstance(identifier, int) or not isinstance(name, str):
                    raise PaperlessUnavailableError("malformed taxonomy response")
                resolved.append(TaxonomyItem(identifier, name))
            return tuple(resolved)

        return Taxonomy(
            tags=items(tags),
            correspondents=items(correspondents),
            document_types=items(document_types),
        )

    async def submit_document(
        self,
        path: Path,
        filename: str,
        media_type: str,
        guidance: MetadataGuidance,
    ) -> UUID:
        """Submit once; any transport ambiguity is fail-closed."""
        fields: dict[str, Any] = {"tags": [str(tag) for tag in guidance.tag_ids]}
        if guidance.correspondent_id is not None:
            fields["correspondent"] = str(guidance.correspondent_id)
        if guidance.document_type_id is not None:
            fields["document_type"] = str(guidance.document_type_id)
        try:
            with path.open("rb") as stream:
                response = await self._client.post(
                    "api/documents/post_document/",
                    data=fields,
                    files={"document": (filename, stream, media_type)},
                )
        except (OSError, httpx.HTTPError) as error:
            raise AmbiguousSubmissionError("document POST outcome is unknown") from error
        self._raise_status(response)
        try:
            raw = response.json()
            task_id = raw if isinstance(raw, str) else raw["task_id"]
            return UUID(task_id)
        except (KeyError, TypeError, ValueError) as error:
            raise AmbiguousSubmissionError("document POST returned no durable task ID") from error

    async def get_task(self, task_id: UUID) -> PaperlessTask:
        """Read and normalize one Paperless consumption task."""
        try:
            response = await self._client.get("api/tasks/", params={"task_id": str(task_id)})
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless task unavailable") from error
        self._raise_status(response)
        try:
            payload = response.json()
            values = payload.get("results", payload) if isinstance(payload, dict) else payload
            item = values[0] if isinstance(values, list) else values
            raw_state = str(item.get("status", item.get("state", ""))).casefold()
            related = item.get("related_document_ids", [])
            document_id = related[0] if isinstance(related, list) and related else None
            result_data = item.get("result_data", {})
            if document_id is None and isinstance(result_data, dict):
                document_id = result_data.get("document_id", result_data.get("duplicate_of"))
            if document_id is None:
                document_id = item.get("related_document", item.get("document_id"))
            message = item.get("message", item.get("result"))
        except (AttributeError, IndexError, TypeError, ValueError) as error:
            raise PaperlessUnavailableError("malformed task response") from error
        state = {
            "pending": TaskState.PENDING,
            "received": TaskState.PENDING,
            "started": TaskState.STARTED,
            "running": TaskState.STARTED,
            "success": TaskState.SUCCESS,
            "succeeded": TaskState.SUCCESS,
            "failure": TaskState.FAILURE,
            "failed": TaskState.FAILURE,
        }.get(raw_state, TaskState.UNKNOWN)
        return PaperlessTask(
            task_id=task_id,
            state=state,
            document_id=DocumentId(document_id) if isinstance(document_id, int) else None,
            message=message if isinstance(message, str) else None,
        )

    async def add_note(self, document_id: int, note: str) -> None:
        """Create one native note idempotently across restart recovery."""
        try:
            endpoint = f"api/documents/{document_id}/notes/"
            existing = await self._client.get(endpoint)
            self._raise_status(existing)
            payload = existing.json()
            if isinstance(payload, list) and any(
                isinstance(item, dict) and item.get("note") == note for item in payload
            ):
                return
            response = await self._client.post(endpoint, json={"note": note})
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless note unavailable") from error
        except ValueError as error:
            raise PaperlessUnavailableError("malformed note response") from error
        self._raise_status(response)

    async def download(
        self, document_id: int, destination: Path, *, archived: bool = False
    ) -> Download:
        """Stream an original or archived file to a caller-supplied UUID path."""
        params = {"follow_formatting": "true"}
        if not archived:
            params["original"] = "true"
        try:
            async with self._client.stream(
                "GET", f"api/documents/{document_id}/download/", params=params
            ) as response:
                self._raise_status(response)
                fallback = f"document-{document_id}.pdf" if archived else f"document-{document_id}"
                filename = _filename_from_headers(response.headers, fallback)
                media_type = response.headers.get("content-type", "application/octet-stream")
                size = 0
                with destination.open("xb") as stream:
                    destination.chmod(0o600)  # noqa: ASYNC240
                    async for chunk in response.aiter_bytes():
                        stream.write(chunk)
                        size += len(chunk)
        except httpx.HTTPError as error:
            destination.unlink(missing_ok=True)  # noqa: ASYNC240
            raise PaperlessUnavailableError("Paperless download unavailable") from error
        except OSError as error:
            destination.unlink(missing_ok=True)  # noqa: ASYNC240
            raise PaperlessUnavailableError("download staging unavailable") from error
        return Download(destination, filename, media_type, size)

    def document_url(self, document_id: int) -> str:
        """Return the browser UI link without embedding credentials."""
        base = str(self._settings.paperless_public_url).rstrip("/")
        return f"{base}/documents/{document_id}/details"

    def original_download_url(self, document_id: int) -> str:
        """Return the user's session-authenticated original download link."""
        base = str(self._settings.paperless_public_url).rstrip("/")
        return f"{base}/api/documents/{document_id}/download/?original=true&follow_formatting=true"
