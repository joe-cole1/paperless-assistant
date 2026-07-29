"""HTTP adapter for the narrow Paperless-ngx v3 API surface."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import httpx
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    AmbiguousSubmissionError,
    PaperlessAIConfigurationError,
    PaperlessAIDisabledError,
    PaperlessAITimeoutError,
    PaperlessAITransportError,
    PaperlessAuthenticationError,
    PaperlessPermissionError,
    PaperlessUnavailableError,
)
from paperless_assistant.models import (
    AISuggestions,
    ChatResult,
    Document,
    DocumentId,
    DocumentUpdate,
    Download,
    MetadataGuidance,
    PaperlessTask,
    SuggestedDate,
    TaskState,
    Taxonomy,
    TaxonomyCapabilities,
    TaxonomyItem,
    TaxonomyKind,
)

CHAT_METADATA_DELIMITER = "\n\n__PAPERLESS_CHAT_METADATA__"
PAPERLESS_NO_CONTENT = "Sorry, I couldn't find any content to answer your question."
PAPERLESS_CHAT_ERROR = "Sorry, something went wrong while generating a response."
PAPERLESS_ERROR_LOG_LIMIT = 4096

logger = logging.getLogger(__name__)

_SECRET_VALUE = re.compile(
    r"""(?ix)
    (
        ["']?
        (?:authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|
        password|secret|token)
        ["']?
        \s*[:=]\s*
    )
    ("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)
    """
)
_AUTH_SCHEME_VALUE = re.compile(r"(?i)\b(token|bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PATH_IDENTIFIER = re.compile(r"/(?:\d+|[0-9a-f]{8}-[0-9a-f-]{27,})/")


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _integer_tuple(payload: Mapping[str, Any], key: str) -> tuple[int, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, int) for item in value):
        raise PaperlessUnavailableError("malformed Paperless response")
    return tuple(value)


def _string_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PaperlessUnavailableError("malformed Paperless response")
    return tuple(value)


def _document(payload: object) -> Document:
    if not isinstance(payload, Mapping):
        raise PaperlessUnavailableError("malformed document response")
    identifier = payload.get("id")
    title = payload.get("title")
    if not isinstance(identifier, int) or not isinstance(title, str):
        raise PaperlessUnavailableError("malformed document response")
    original = payload.get("original_file_name")
    archived = payload.get("archived_file_name")
    raw_storage_path = payload.get("storage_path")
    raw_correspondent = payload.get("correspondent")
    raw_document_type = payload.get("document_type")
    scalar_taxonomy = (raw_correspondent, raw_document_type, raw_storage_path)
    if any(value is not None and not isinstance(value, int) for value in scalar_taxonomy):
        raise PaperlessUnavailableError("malformed document response")
    return Document(
        id=DocumentId(identifier),
        title=title,
        created=_parse_date(payload.get("created")),
        original_filename=original if isinstance(original, str) else None,
        archived_filename=archived if isinstance(archived, str) else None,
        modified=_parse_datetime(payload.get("modified")),
        tag_ids=_integer_tuple(payload, "tags"),
        correspondent_id=raw_correspondent,
        document_type_id=raw_document_type,
        storage_path_id=raw_storage_path,
    )


def sanitize_paperless_error(value: str | bytes) -> tuple[str, bool]:
    """Bound, escape, and redact credential-shaped values from Paperless diagnostics."""
    raw = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
    bounded = raw[:PAPERLESS_ERROR_LOG_LIMIT]
    text = bounded.decode("utf-8", errors="replace")
    text = _CONTROL_CHARACTERS.sub("\ufffd", text)
    text = _SECRET_VALUE.sub(r"\1[REDACTED]", text)
    return _AUTH_SCHEME_VALUE.sub(r"\1 [REDACTED]", text), len(raw) > len(bounded)


def _operation(response: httpx.Response) -> str:
    try:
        method = response.request.method
        path = response.request.url.path
    except RuntimeError:
        return "paperless_request"
    return f"{method} {_PATH_IDENTIFIER.sub('/{id}/', path)}"


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

    def _headers(self, token: SecretStr | None = None) -> dict[str, str] | None:
        if token is None:
            return None
        return {"Authorization": f"Token {token.get_secret_value()}"}

    async def validate_token(self, token: SecretStr) -> bool:
        """Validate a candidate Paperless API token via a harmless authenticated request."""
        try:
            response = await self._client.get(
                "api/documents/",
                params={"page_size": 1},
                headers=self._headers(token),
            )
            return response.status_code == 200
        except httpx.HTTPError, PaperlessUnavailableError, PaperlessAuthenticationError:
            return False

    async def close(self) -> None:
        """Close the owned connection pool."""
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    async def _log_error_response(response: httpx.Response) -> None:
        try:
            raw = response.content
        except httpx.ResponseNotRead:
            buffered = bytearray()
            async for chunk in response.aiter_bytes():
                remaining = PAPERLESS_ERROR_LOG_LIMIT + 1 - len(buffered)
                buffered.extend(chunk[:remaining])
                if len(buffered) > PAPERLESS_ERROR_LOG_LIMIT:
                    break
            raw = bytes(buffered)
        sanitized, truncated = sanitize_paperless_error(raw)
        logger.warning(
            "paperless_request_failed",
            extra={
                "operation": _operation(response),
                "status_code": response.status_code,
                "paperless_error": sanitized,
                "truncated": truncated,
            },
        )

    @classmethod
    async def _raise_status(cls, response: httpx.Response) -> None:
        if not response.is_error:
            return
        await cls._log_error_response(response)
        if response.status_code == 401:
            raise PaperlessAuthenticationError("Paperless authentication failed")
        if response.status_code == 403:
            raise PaperlessPermissionError("Paperless permission denied")
        raise PaperlessUnavailableError("Paperless request failed")

    async def chat(
        self, question: str, document_id: int | None = None, *, token: SecretStr | None = None
    ) -> ChatResult:
        """Complete one native Paperless streamed chat call."""
        try:
            async with self._client.stream(
                "POST",
                "api/documents/chat/",
                json={"q": question, "document_id": document_id},
                headers=self._headers(token),
                timeout=httpx.Timeout(self._settings.paperless_chat_timeout_seconds),
            ) as response:
                await self._raise_status(response)
                chunks = [chunk async for chunk in response.aiter_text()]
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless chat unavailable") from error
        return parse_chat_response("".join(chunks))

    async def _paginated(
        self, endpoint: str, *, token: SecretStr | None = None
    ) -> tuple[dict[str, Any], ...]:
        page = 1
        results: list[dict[str, Any]] = []
        while True:
            try:
                response = await self._client.get(
                    endpoint,
                    params={"page": page, "page_size": 100},
                    headers=self._headers(token),
                )
            except httpx.HTTPError as error:
                raise PaperlessUnavailableError("Paperless API unavailable") from error

            await self._raise_status(response)
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

    async def search_documents(
        self, query: str, limit: int = 3, *, token: SecretStr | None = None
    ) -> tuple[Document, ...]:
        """Use Paperless native full-text search without interpreting the query."""
        try:
            response = await self._client.get(
                "api/documents/",
                params={"query": query, "page_size": min(limit, 3)},
                headers=self._headers(token),
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless search unavailable") from error
        await self._raise_status(response)
        try:
            payload = response.json()
            results = payload["results"]
        except (KeyError, TypeError, ValueError) as error:
            raise PaperlessUnavailableError("malformed search response") from error
        if not isinstance(results, list):
            raise PaperlessUnavailableError("malformed search response")
        return tuple(_document(item) for item in results[: min(limit, 3)])

    async def find_similar_documents(
        self,
        document_id: int,
        limit: int = 3,
        *,
        token: SecretStr | None = None,
    ) -> tuple[Document, ...]:
        """Return a bounded native ``more_like_id`` search without the source document."""
        bounded_limit = min(max(limit, 0), 3)
        try:
            response = await self._client.get(
                "api/documents/",
                params={"more_like_id": document_id, "page_size": 4},
                headers=self._headers(token),
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless similar search unavailable") from error
        await self._raise_status(response)
        try:
            payload = response.json()
            results = payload["results"]
        except (KeyError, TypeError, ValueError) as error:
            raise PaperlessUnavailableError("malformed similar search response") from error
        if not isinstance(results, list):
            raise PaperlessUnavailableError("malformed similar search response")
        documents = (
            document
            for document in (_document(item) for item in results)
            if int(document.id) != document_id
        )
        return tuple(documents)[:bounded_limit]

    async def get_document(self, document_id: int, *, token: SecretStr | None = None) -> Document:
        """Fetch current visible metadata for a referenced document."""
        try:
            response = await self._client.get(
                f"api/documents/{document_id}/", headers=self._headers(token)
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless document unavailable") from error
        await self._raise_status(response)
        try:
            return _document(response.json())
        except ValueError as error:
            raise PaperlessUnavailableError("malformed document response") from error

    async def get_taxonomy(self, *, token: SecretStr | None = None) -> Taxonomy:
        """Read every visible supported taxonomy category in parallel."""
        tags, correspondents, document_types, storage_paths = await asyncio.gather(
            self._paginated("api/tags/", token=token),
            self._paginated("api/correspondents/", token=token),
            self._paginated("api/document_types/", token=token),
            self._paginated("api/storage_paths/", token=token),
        )

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
            storage_paths=items(storage_paths),
        )

    async def get_taxonomy_capabilities(
        self, *, token: SecretStr | None = None
    ) -> TaxonomyCapabilities:
        """Read invoking-user taxonomy creation permissions from Paperless."""
        try:
            response = await self._client.get("api/ui_settings/", headers=self._headers(token))
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless capabilities unavailable") from error
        await self._raise_status(response)
        try:
            payload = response.json()
            permissions = payload["permissions"]
        except (KeyError, TypeError, ValueError) as error:
            raise PaperlessUnavailableError("malformed capabilities response") from error
        if not isinstance(permissions, list) or not all(
            isinstance(value, str) for value in permissions
        ):
            raise PaperlessUnavailableError("malformed capabilities response")
        visible = frozenset(permissions)
        return TaxonomyCapabilities(
            add_tags="add_tag" in visible,
            add_correspondents="add_correspondent" in visible,
            add_document_types="add_documenttype" in visible,
            add_storage_paths="add_storagepath" in visible,
        )

    @staticmethod
    def _taxonomy_endpoint(kind: TaxonomyKind) -> str:
        return {
            TaxonomyKind.TAG: "api/tags/",
            TaxonomyKind.CORRESPONDENT: "api/correspondents/",
            TaxonomyKind.DOCUMENT_TYPE: "api/document_types/",
            TaxonomyKind.STORAGE_PATH: "api/storage_paths/",
        }[kind]

    async def find_taxonomy_items(
        self,
        kind: TaxonomyKind,
        name: str,
        *,
        token: SecretStr | None = None,
    ) -> tuple[TaxonomyItem, ...]:
        """Find all visible exact case-insensitive taxonomy-name matches."""
        endpoint = self._taxonomy_endpoint(kind)
        try:
            response = await self._client.get(
                endpoint,
                params={"name__iexact": name, "page_size": 100},
                headers=self._headers(token),
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless taxonomy lookup unavailable") from error
        await self._raise_status(response)
        try:
            payload = response.json()
            values = payload["results"]
        except (KeyError, TypeError, ValueError) as error:
            raise PaperlessUnavailableError("malformed taxonomy lookup response") from error
        if not isinstance(values, list):
            raise PaperlessUnavailableError("malformed taxonomy lookup response")
        resolved: list[TaxonomyItem] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise PaperlessUnavailableError("malformed taxonomy lookup response")
            identifier = value.get("id")
            item_name = value.get("name")
            if not isinstance(identifier, int) or not isinstance(item_name, str):
                raise PaperlessUnavailableError("malformed taxonomy lookup response")
            if item_name.casefold() == name.casefold():
                resolved.append(TaxonomyItem(identifier, item_name))
        return tuple(resolved)

    async def create_taxonomy_item(
        self,
        kind: TaxonomyKind,
        name: str,
        *,
        storage_path: str | None = None,
        token: SecretStr | None = None,
    ) -> TaxonomyItem:
        """Create one separately confirmed taxonomy object with matching disabled."""
        fields: dict[str, Any] = {"name": name}
        if kind is TaxonomyKind.STORAGE_PATH:
            if storage_path is None:
                raise ValueError("storage_path is required")
            fields["path"] = storage_path
        else:
            fields.update({"matching_algorithm": 0, "match": ""})
        try:
            response = await self._client.post(
                self._taxonomy_endpoint(kind),
                json=fields,
                headers=self._headers(token),
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless taxonomy creation unavailable") from error
        await self._raise_status(response)
        try:
            payload = response.json()
        except ValueError as error:
            raise PaperlessUnavailableError("malformed taxonomy creation response") from error
        if not isinstance(payload, Mapping):
            raise PaperlessUnavailableError("malformed taxonomy creation response")
        identifier = payload.get("id")
        item_name = payload.get("name")
        if not isinstance(identifier, int) or not isinstance(item_name, str):
            raise PaperlessUnavailableError("malformed taxonomy creation response")
        return TaxonomyItem(identifier, item_name)

    async def get_document_tag_ids(
        self, document_id: int, *, token: SecretStr | None = None
    ) -> tuple[int, ...] | None:
        """Fetch tag IDs, returning ``None`` only when the document does not exist."""
        try:
            response = await self._client.get(
                f"api/documents/{document_id}/", headers=self._headers(token)
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless document unavailable") from error
        if response.status_code == 404:
            return None
        await self._raise_status(response)
        try:
            payload = response.json()
        except ValueError as error:
            raise PaperlessUnavailableError("malformed document response") from error
        if not isinstance(payload, Mapping):
            raise PaperlessUnavailableError("malformed document response")
        return _integer_tuple(payload, "tags")

    async def get_documents_tag_ids(
        self,
        document_ids: tuple[int, ...],
        *,
        token: SecretStr | None = None,
    ) -> dict[int, tuple[int, ...] | None]:
        """Fetch document tags in bounded list requests instead of one request per document."""
        unique_ids = tuple(dict.fromkeys(document_ids))
        resolved: dict[int, tuple[int, ...] | None] = dict.fromkeys(unique_ids)
        for offset in range(0, len(unique_ids), 100):
            batch = unique_ids[offset : offset + 100]
            try:
                response = await self._client.get(
                    "api/documents/",
                    params={"id__in": ",".join(str(value) for value in batch), "page_size": 100},
                    headers=self._headers(token),
                )
            except httpx.HTTPError as error:
                raise PaperlessUnavailableError("Paperless documents unavailable") from error
            await self._raise_status(response)
            try:
                payload = response.json()
                values = payload["results"]
            except (KeyError, TypeError, ValueError) as error:
                raise PaperlessUnavailableError("malformed document response") from error
            if not isinstance(values, list):
                raise PaperlessUnavailableError("malformed document response")
            for value in values:
                if not isinstance(value, Mapping) or not isinstance(value.get("id"), int):
                    raise PaperlessUnavailableError("malformed document response")
                identifier = value["id"]
                if identifier not in resolved:
                    raise PaperlessUnavailableError("malformed document response")
                resolved[identifier] = _integer_tuple(value, "tags")
        return resolved

    async def get_ai_suggestions(
        self, document_id: int, *, token: SecretStr | None = None
    ) -> AISuggestions:
        """Synchronously trigger and parse Paperless 3.0.4's native LLM endpoint."""
        try:
            response = await self._client.get(
                f"api/documents/{document_id}/ai_suggestions/",
                headers=self._headers(token),
                timeout=httpx.Timeout(self._settings.paperless_ai_suggestions_timeout_seconds),
            )
        except httpx.TimeoutException as error:
            logger.warning(
                "paperless_ai_suggestions_failed",
                extra={"reason": "assistant_timeout", "error_type": type(error).__name__},
            )
            raise PaperlessAITimeoutError("Paperless AI suggestions timed out") from error
        except httpx.HTTPError as error:
            logger.warning(
                "paperless_ai_suggestions_failed",
                extra={"reason": "transport", "error_type": type(error).__name__},
            )
            raise PaperlessAITransportError(
                "Paperless AI suggestions transport unavailable"
            ) from error
        if response.status_code == 400:
            if b"AI is required for this feature" in response.content:
                await self._log_error_response(response)
                logger.warning(
                    "paperless_ai_suggestions_failed",
                    extra={"reason": "disabled", "status_code": response.status_code},
                )
                raise PaperlessAIDisabledError("Paperless AI is disabled")
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if (
                isinstance(payload, Mapping)
                and isinstance(payload.get("ai"), list)
                and payload["ai"]
            ):
                await self._log_error_response(response)
                logger.warning(
                    "paperless_ai_suggestions_failed",
                    extra={"reason": "invalid_configuration", "status_code": response.status_code},
                )
                raise PaperlessAIConfigurationError("Paperless AI configuration is invalid")
        if response.status_code == 503:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if (
                isinstance(payload, Mapping)
                and isinstance(payload.get("ai"), list)
                and payload["ai"]
            ):
                await self._log_error_response(response)
                logger.warning(
                    "paperless_ai_suggestions_failed",
                    extra={"reason": "upstream_timeout", "status_code": response.status_code},
                )
                raise PaperlessAITimeoutError("Paperless AI backend timed out")
        await self._raise_status(response)
        try:
            payload = response.json()
        except ValueError as error:
            raise PaperlessUnavailableError("malformed AI suggestions response") from error
        if not isinstance(payload, Mapping):
            raise PaperlessUnavailableError("malformed AI suggestions response")
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise PaperlessUnavailableError("malformed AI suggestions response")
        dates = tuple(
            SuggestedDate(raw=value, value=_parse_date(value))
            for value in _string_tuple(payload, "dates")
        )

        return AISuggestions(
            title=title or None,
            correspondent_ids=_integer_tuple(payload, "correspondents"),
            document_type_ids=_integer_tuple(payload, "document_types"),
            storage_path_ids=_integer_tuple(payload, "storage_paths"),
            tag_ids=_integer_tuple(payload, "tags"),
            dates=dates,
            suggested_correspondents=_string_tuple(payload, "suggested_correspondents"),
            suggested_document_types=_string_tuple(payload, "suggested_document_types"),
            suggested_storage_paths=_string_tuple(payload, "suggested_storage_paths"),
            suggested_tags=_string_tuple(payload, "suggested_tags"),
        )

    async def update_document(
        self, document_id: int, updates: DocumentUpdate, *, token: SecretStr | None = None
    ) -> None:
        """Apply explicit metadata updates to a document."""
        fields: dict[str, Any] = {}
        if updates.title is not None:
            fields["title"] = updates.title
        if updates.correspondent_id is not None:
            fields["correspondent"] = updates.correspondent_id
        if updates.document_type_id is not None:
            fields["document_type"] = updates.document_type_id
        if updates.storage_path_id is not None:
            fields["storage_path"] = updates.storage_path_id
        if updates.created is not None:
            fields["created"] = updates.created.isoformat()
        if updates.tag_ids is not None:
            fields["tags"] = list(updates.tag_ids)

        if not fields:
            return

        try:
            response = await self._client.patch(
                f"api/documents/{document_id}/", json=fields, headers=self._headers(token)
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless update unavailable") from error
        await self._raise_status(response)

    async def modify_document_tags(
        self,
        document_id: int,
        *,
        add_tag_ids: tuple[int, ...],
        remove_tag_ids: tuple[int, ...] = (),
        token: SecretStr | None = None,
    ) -> None:
        """Add or remove only explicitly selected tags through Paperless bulk edit."""
        if not add_tag_ids and not remove_tag_ids:
            return
        try:
            response = await self._client.post(
                "api/documents/bulk_edit/",
                json={
                    "documents": [document_id],
                    "method": "modify_tags",
                    "parameters": {
                        "add_tags": list(add_tag_ids),
                        "remove_tags": list(remove_tag_ids),
                    },
                },
                headers=self._headers(token),
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless tag update unavailable") from error
        await self._raise_status(response)

    async def submit_document(
        self,
        path: Path,
        filename: str,
        media_type: str,
        guidance: MetadataGuidance,
        *,
        token: SecretStr | None = None,
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
                    headers=self._headers(token),
                )
        except (OSError, httpx.HTTPError) as error:
            raise AmbiguousSubmissionError("document POST outcome is unknown") from error
        await self._raise_status(response)
        try:
            raw = response.json()
            task_id = raw if isinstance(raw, str) else raw["task_id"]
            return UUID(task_id)
        except (KeyError, TypeError, ValueError) as error:
            raise AmbiguousSubmissionError("document POST returned no durable task ID") from error

    async def get_task(self, task_id: UUID, *, token: SecretStr | None = None) -> PaperlessTask:
        """Read and normalize one Paperless consumption task."""
        try:
            response = await self._client.get(
                "api/tasks/",
                params={"task_id": str(task_id)},
                headers=self._headers(token),
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless task unavailable") from error
        await self._raise_status(response)
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

    async def add_note(
        self, document_id: int, note: str, *, token: SecretStr | None = None
    ) -> None:
        """Create one native note idempotently across restart recovery."""
        try:
            endpoint = f"api/documents/{document_id}/notes/"
            existing = await self._client.get(endpoint, headers=self._headers(token))
            await self._raise_status(existing)
            payload = existing.json()
            if isinstance(payload, list) and any(
                isinstance(item, dict) and item.get("note") == note for item in payload
            ):
                return
            response = await self._client.post(
                endpoint, json={"note": note}, headers=self._headers(token)
            )
        except httpx.HTTPError as error:
            raise PaperlessUnavailableError("Paperless note unavailable") from error
        except ValueError as error:
            raise PaperlessUnavailableError("malformed note response") from error
        await self._raise_status(response)

    async def download(
        self,
        document_id: int,
        destination: Path,
        *,
        archived: bool = False,
        token: SecretStr | None = None,
    ) -> Download:
        """Stream an original or archived file to a caller-supplied UUID path."""
        params = {"follow_formatting": "true"}
        if not archived:
            params["original"] = "true"
        try:
            async with self._client.stream(
                "GET",
                f"api/documents/{document_id}/download/",
                params=params,
                headers=self._headers(token),
            ) as response:
                await self._raise_status(response)
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
