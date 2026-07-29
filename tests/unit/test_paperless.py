"""Contract tests for the narrow Paperless v3 HTTP adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    AmbiguousPaperlessMutationError,
    AmbiguousSubmissionError,
    DuplicateUploadError,
    PaperlessAIConfigurationError,
    PaperlessAIDisabledError,
    PaperlessAITimeoutError,
    PaperlessAITransportError,
    PaperlessAuthenticationError,
    PaperlessPermissionError,
    PaperlessUnavailableError,
)
from paperless_assistant.models import (
    DocumentId,
    DocumentUpdate,
    MetadataGuidance,
    SuggestedDate,
    TaskState,
    TaxonomyItem,
    TaxonomyKind,
)
from paperless_assistant.paperless import (
    CHAT_METADATA_DELIMITER,
    HttpPaperlessGateway,
    _confirmed_duplicate,
    _document,
    _integer_tuple,
    _operation,
    _parse_date,
    _parse_datetime,
    _string_tuple,
    parse_chat_response,
)


def _gateway(
    settings_factory: Callable[..., Settings],
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpPaperlessGateway:
    client = httpx.AsyncClient(
        base_url="http://paperless.test/",
        transport=httpx.MockTransport(handler),
    )
    return HttpPaperlessGateway(settings_factory(), client)


def _json_response(value: object) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=value)

    return handler


def _fixed_response(value: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return value

    return handler


def test_parse_chat_response_trailer_and_malformed_metadata() -> None:
    parsed = parse_chat_response(
        "Answer"
        + CHAT_METADATA_DELIMITER
        + '{"references":[{"id":7,"title":"Seven"},{"id":8},{"bad":9}]}'
    )
    malformed = parse_chat_response("Partial" + CHAT_METADATA_DELIMITER + "{bad")

    assert parsed.answer == "Answer"
    assert parsed.document_ids == (DocumentId(7), DocumentId(8))
    assert malformed.answer == "Partial"
    assert malformed.document_ids == ()
    assert parse_chat_response("No trailer").answer == "No trailer"


def test_duplicate_marker_rejects_non_mapping_payload() -> None:
    assert not _confirmed_duplicate(["duplicate_of", 91])


def test_document_payload_validation_and_dates() -> None:
    assert _parse_date(None) is None
    assert _parse_date("") is None
    with pytest.raises(PaperlessUnavailableError, match="malformed document"):
        _document([])
    with pytest.raises(PaperlessUnavailableError, match="malformed document"):
        _document({"id": "7", "title": 8})
    with pytest.raises(PaperlessUnavailableError, match="malformed document"):
        _document({"id": 7, "title": "Synthetic", "correspondent": "bad"})
    assert _parse_datetime("2026-07-28T12:00:00") is not None
    assert _parse_datetime("invalid") is None
    assert _operation(httpx.Response(500)) == "paperless_request"
    with pytest.raises(PaperlessUnavailableError, match="malformed"):
        _integer_tuple({"values": "bad"}, "values")
    with pytest.raises(PaperlessUnavailableError, match="malformed"):
        _string_tuple({"values": [1]}, "values")


@pytest.mark.asyncio
async def test_owned_gateway_client_closes(settings_factory: Callable[..., Settings]) -> None:
    gateway = HttpPaperlessGateway(settings_factory())
    assert not gateway._client.is_closed

    await gateway.close()

    assert gateway._client.is_closed


@pytest.mark.asyncio
async def test_get_document_tag_ids(settings_factory: Callable[..., Settings]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents/100/":
            return httpx.Response(200, json={"id": 100, "tags": [1, 2, 5]})
        if request.url.path == "/api/documents/101/":
            return httpx.Response(200, json={"id": 101, "tags": "invalid"})
        if request.url.path == "/api/documents/404/":
            return httpx.Response(404)
        return httpx.Response(500)

    gateway = _gateway(settings_factory, handler)
    assert await gateway.get_document_tag_ids(100) == (1, 2, 5)
    with pytest.raises(PaperlessUnavailableError, match="malformed"):
        await gateway.get_document_tag_ids(101)
    assert await gateway.get_document_tag_ids(404) is None
    with pytest.raises(PaperlessUnavailableError, match="failed"):
        await gateway.get_document_tag_ids(500)


@pytest.mark.asyncio
async def test_get_documents_tag_ids_batches_and_distinguishes_missing(
    settings_factory: Callable[..., Settings],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents/"
        assert request.url.params["id__in"] == "100,101,404"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": 100, "tags": [1, 2]},
                    {"id": 101, "tags": []},
                ]
            },
        )

    result = await _gateway(settings_factory, handler).get_documents_tag_ids((100, 101, 404, 100))

    assert result == {100: (1, 2), 101: (), 404: None}


@pytest.mark.asyncio
async def test_chat_search_documents_and_urls(
    settings_factory: Callable[..., Settings],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents/chat/":
            assert request.method == "POST"
            assert b'"q":"unchanged question"' in request.content
            return httpx.Response(
                200,
                text="Native answer"
                + CHAT_METADATA_DELIMITER
                + '{"references":[{"id":7,"title":"Synthetic"}]}',
            )
        if request.url.path == "/api/documents/" and "query=" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 7, "title": "Synthetic", "created": "2024-01-02"},
                        {"id": 8, "title": "Other", "created": "bad"},
                    ]
                },
            )
        if request.url.path == "/api/documents/7/":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "title": "Synthetic",
                    "created": "2024-01-02T00:00:00Z",
                    "original_file_name": "original.pdf",
                    "archived_file_name": "archive.pdf",
                },
            )
        raise AssertionError(request.url)

    gateway = _gateway(settings_factory, handler)
    chat = await gateway.chat("unchanged question")
    search = await gateway.search_documents("unchanged question")
    document = await gateway.get_document(7)

    assert chat.answer == "Native answer"
    assert chat.document_ids == (DocumentId(7),)
    assert len(search) == 2
    assert search[0].created is not None
    assert search[1].created is None
    assert document.original_filename == "original.pdf"
    assert gateway.document_url(7).endswith("/documents/7/details")
    assert "original=true" in gateway.original_download_url(7)
    await gateway.close()


@pytest.mark.asyncio
async def test_find_similar_documents_uses_user_token_and_bounds_results(
    settings_factory: Callable[..., Settings],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/documents/"
        assert dict(request.url.params) == {"more_like_id": "7", "page_size": "4"}
        assert request.headers["Authorization"] == "Token linked-user-token"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": 7, "title": "Source"},
                    {"id": 8, "title": "First"},
                    {"id": 9, "title": "Second"},
                    {"id": 10, "title": "Third"},
                ]
            },
        )

    documents = await _gateway(settings_factory, handler).find_similar_documents(
        7,
        token=SecretStr("linked-user-token"),
    )
    empty = await _gateway(settings_factory, handler).find_similar_documents(
        7,
        limit=0,
        token=SecretStr("linked-user-token"),
    )
    bounded = await _gateway(settings_factory, handler).find_similar_documents(
        7,
        limit=99,
        token=SecretStr("linked-user-token"),
    )

    assert tuple(int(document.id) for document in documents) == (8, 9, 10)
    assert empty == ()
    assert tuple(int(document.id) for document in bounded) == (8, 9, 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 404])
async def test_find_similar_documents_handles_denied_or_missing_source(
    settings_factory: Callable[..., Settings],
    status_code: int,
) -> None:
    gateway = _gateway(
        settings_factory,
        lambda _: httpx.Response(status_code, text="sensitive upstream detail"),
    )

    with pytest.raises(PaperlessUnavailableError):
        await gateway.find_similar_documents(7)


@pytest.mark.asyncio
async def test_find_similar_documents_rejects_transport_failure(
    settings_factory: Callable[..., Settings],
) -> None:
    def transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("synthetic", request=request)

    unavailable = _gateway(settings_factory, transport_error)
    with pytest.raises(PaperlessUnavailableError, match="unavailable"):
        await unavailable.find_similar_documents(7)


@pytest.mark.asyncio
async def test_find_similar_documents_rejects_malformed_responses(
    settings_factory: Callable[..., Settings],
) -> None:
    responses = iter(
        [
            httpx.Response(200, json={}),
            httpx.Response(200, json={"results": {}}),
        ]
    )
    malformed = _gateway(settings_factory, lambda _: next(responses))
    with pytest.raises(PaperlessUnavailableError, match="malformed"):
        await malformed.find_similar_documents(7)
    with pytest.raises(PaperlessUnavailableError, match="malformed"):
        await malformed.find_similar_documents(7)


@pytest.mark.asyncio
async def test_taxonomy_pagination(settings_factory: Callable[..., Settings]) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        page = request.url.params.get("page")
        if request.url.path == "/api/tags/" and page == "1":
            return httpx.Response(
                200,
                json={"results": [{"id": 1, "name": "Discord"}], "next": "page2"},
            )
        if request.url.path == "/api/tags/":
            return httpx.Response(
                200, json={"results": [{"id": 2, "name": "Travel"}], "next": None}
            )
        return httpx.Response(
            200,
            json={"results": [{"id": 3, "name": "Synthetic"}], "next": None},
        )

    taxonomy = await _gateway(settings_factory, handler).get_taxonomy()

    assert [item.name for item in taxonomy.tags] == ["Discord", "Travel"]
    assert taxonomy.correspondents[0].name == "Synthetic"
    assert taxonomy.document_types[0].name == "Synthetic"
    assert taxonomy.storage_paths[0].name == "Synthetic"
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_taxonomy_capabilities_lookup_and_creation(
    settings_factory: Callable[..., Settings],
) -> None:
    requests: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, payload))
        if request.url.path == "/api/ui_settings/":
            return httpx.Response(
                200,
                json={
                    "permissions": [
                        "add_tag",
                        "add_correspondent",
                        "add_documenttype",
                        "add_storagepath",
                    ]
                },
            )
        if request.url.path == "/api/tags/" and request.method == "GET":
            assert request.url.params["name__iexact"] == "Household"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 7, "name": "HOUSEHOLD"},
                        {"id": 8, "name": "Other"},
                    ]
                },
            )
        if request.url.path == "/api/tags/" and request.method == "POST":
            return httpx.Response(201, json={"id": 9, "name": payload["name"]})
        if request.url.path == "/api/storage_paths/" and request.method == "POST":
            return httpx.Response(201, json={"id": 10, "name": payload["name"]})
        raise AssertionError(request.url)

    gateway = _gateway(settings_factory, handler)
    capabilities = await gateway.get_taxonomy_capabilities()
    matches = await gateway.find_taxonomy_items(TaxonomyKind.TAG, "Household")
    tag = await gateway.create_taxonomy_item(TaxonomyKind.TAG, "New Tag")
    storage = await gateway.create_taxonomy_item(
        TaxonomyKind.STORAGE_PATH,
        "Personal",
        storage_path="Personal/{created_year}",
    )

    assert capabilities.add_tags
    assert capabilities.add_correspondents
    assert capabilities.add_document_types
    assert capabilities.add_storage_paths
    assert matches == (TaxonomyItem(7, "HOUSEHOLD"),)
    assert tag == TaxonomyItem(9, "New Tag")
    assert storage == TaxonomyItem(10, "Personal")
    assert ("POST", "/api/tags/", {"name": "New Tag", "matching_algorithm": 0, "match": ""}) in (
        requests
    )
    assert (
        "POST",
        "/api/storage_paths/",
        {"name": "Personal", "path": "Personal/{created_year}"},
    ) in requests

    with pytest.raises(ValueError, match="storage_path"):
        await gateway.create_taxonomy_item(TaxonomyKind.STORAGE_PATH, "Missing")


@pytest.mark.asyncio
async def test_upload_task_note_and_download(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    task_id = uuid4()
    posted_note = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted_note
        if request.url.path == "/api/documents/post_document/":
            assert b'name="tags"' in request.content
            assert b"synthetic.pdf" in request.content
            return httpx.Response(200, json=str(task_id))
        if request.url.path == "/api/tasks/":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "status": "SUCCESS",
                            "related_document_ids": [44],
                            "message": "done",
                        }
                    ]
                },
            )
        if request.url.path == "/api/documents/44/notes/" and request.method == "GET":
            return httpx.Response(200, json=[] if not posted_note else [{"note": "guidance"}])
        if request.url.path == "/api/documents/44/notes/":
            posted_note = True
            return httpx.Response(200, json=[{"note": "guidance"}])
        if request.url.path == "/api/documents/44/download/":
            return httpx.Response(
                200,
                content=b"download bytes",
                headers={
                    "content-type": "application/pdf",
                    "content-disposition": "attachment; filename*=UTF-8''Formatted%20Name.pdf",
                },
            )
        raise AssertionError(request.url)

    source = tmp_path / "source"
    source.write_bytes(b"%PDF-1.7")
    gateway = _gateway(settings_factory, handler)
    returned_task = await gateway.submit_document(
        source,
        "synthetic.pdf",
        "application/pdf",
        MetadataGuidance((1, 2), 3, 4),
    )
    task = await gateway.get_task(returned_task)
    await gateway.add_note(44, "guidance")
    await gateway.add_note(44, "guidance")
    download = await gateway.download(44, tmp_path / "spool")

    assert task.state == TaskState.SUCCESS
    assert task.document_id == DocumentId(44)
    assert posted_note
    assert download.filename == "Formatted Name.pdf"
    assert download.size == len(b"download bytes")
    assert download.path.read_bytes() == b"download bytes"


@pytest.mark.asyncio
async def test_task_failure_and_legacy_shapes(
    settings_factory: Callable[..., Settings],
) -> None:
    responses = iter(
        [
            {"results": [{"status": "failure", "result_data": {"duplicate_of": 9}}]},
            [{"status": "STARTED", "related_document": None}],
            {"results": [{"status": "unexpected"}]},
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    gateway = _gateway(settings_factory, handler)
    first = await gateway.get_task(uuid4())
    second = await gateway.get_task(uuid4())
    third = await gateway.get_task(uuid4())

    assert first.state == TaskState.FAILURE
    assert first.document_id is None
    assert first.duplicate_confirmed
    assert second.state == TaskState.STARTED
    assert third.state == TaskState.UNKNOWN


@pytest.mark.asyncio
async def test_submit_document_recognizes_structured_duplicate(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"%PDF-1.7")
    gateway = _gateway(
        settings_factory,
        lambda _: httpx.Response(
            400,
            json={
                "result_data": {"duplicate_of": 91},
                "message": "private upstream title",
            },
        ),
    )

    with pytest.raises(DuplicateUploadError) as raised:
        await gateway.submit_document(
            source,
            "synthetic.pdf",
            "application/pdf",
            MetadataGuidance(),
        )

    assert str(raised.value) == "Paperless confirmed a duplicate upload"


@pytest.mark.asyncio
async def test_submit_document_keeps_ordinary_bad_request_generic(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"%PDF-1.7")
    gateway = _gateway(
        settings_factory,
        lambda _: httpx.Response(
            400,
            json={"detail": "duplicate_of=91 private upstream title"},
        ),
    )

    with pytest.raises(PaperlessUnavailableError) as raised:
        await gateway.submit_document(
            source,
            "synthetic.pdf",
            "application/pdf",
            MetadataGuidance(),
        )

    assert str(raised.value) == "Paperless request failed"


@pytest.mark.asyncio
async def test_submit_document_keeps_malformed_bad_request_generic(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"%PDF-1.7")
    gateway = _gateway(
        settings_factory,
        lambda _: httpx.Response(400, text='{"duplicate_of":'),
    )

    with pytest.raises(PaperlessUnavailableError):
        await gateway.submit_document(
            source,
            "synthetic.pdf",
            "application/pdf",
            MetadataGuidance(),
        )


@pytest.mark.asyncio
async def test_task_duplicate_marker_must_be_a_positive_integer(
    settings_factory: Callable[..., Settings],
) -> None:
    gateway = _gateway(
        settings_factory,
        _json_response(
            {
                "results": [
                    {
                        "status": "failure",
                        "result_data": {"duplicate_of": "private upstream title"},
                    }
                ]
            }
        ),
    )

    task = await gateway.get_task(uuid4())

    assert task.state is TaskState.FAILURE
    assert not task.duplicate_confirmed


@pytest.mark.asyncio
async def test_sanitized_error_boundaries(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"%PDF-1.7")

    auth = _gateway(
        settings_factory,
        lambda _: httpx.Response(
            401,
            text='{"detail":"bad\\nrequest Token mustnotlog123","token":"mustnotlog123"}',
        ),
    )
    with pytest.raises(PaperlessAuthenticationError):
        await auth.get_document(1)
    record = next(item for item in caplog.records if item.message == "paperless_request_failed")
    assert cast(Any, record).paperless_error == (
        '{"detail":"bad\\nrequest Token [REDACTED]","token":[REDACTED]}'
    )
    assert cast(Any, record).operation == "GET /api/documents/{id}/"

    forbidden = _gateway(settings_factory, lambda _: httpx.Response(403, text="Forbidden"))
    with pytest.raises(PaperlessPermissionError):
        await forbidden.get_document(1)

    class UnreadStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"x"
            yield b"x" * 5000

    unread = httpx.Response(
        500,
        stream=UnreadStream(),
        request=httpx.Request("GET", "http://paperless.test/api/documents/1/"),
    )
    await HttpPaperlessGateway._log_error_response(unread)

    class EmptyUnreadStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            if False:
                yield b""

    empty_unread = httpx.Response(
        500,
        stream=EmptyUnreadStream(),
        request=httpx.Request("GET", "http://paperless.test/api/documents/1/"),
    )
    await HttpPaperlessGateway._log_error_response(empty_unread)
    assert "mustnotlog123" not in caplog.text

    malformed = _gateway(settings_factory, lambda _: httpx.Response(200, text="not json"))
    with pytest.raises(PaperlessUnavailableError):
        await malformed.search_documents("q")
    with pytest.raises(AmbiguousSubmissionError):
        await malformed.submit_document(
            source,
            "synthetic.pdf",
            "application/pdf",
            MetadataGuidance(),
        )
    with pytest.raises(PaperlessUnavailableError):
        await malformed.get_task(uuid4())
    with pytest.raises(PaperlessUnavailableError):
        await malformed.add_note(1, "note")

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic", request=request)

    network = _gateway(settings_factory, unavailable)
    with pytest.raises(PaperlessUnavailableError):
        await network.chat("q")
    with pytest.raises(AmbiguousSubmissionError):
        await network.submit_document(
            source,
            "synthetic.pdf",
            "application/pdf",
            MetadataGuidance(),
        )
    with pytest.raises(PaperlessUnavailableError):
        await network.download(1, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()


@pytest.mark.asyncio
async def test_read_endpoints_fail_closed(
    settings_factory: Callable[..., Settings],
) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic", request=request)

    network = _gateway(settings_factory, unavailable)
    with pytest.raises(PaperlessUnavailableError, match="API unavailable"):
        await network.get_taxonomy()
    with pytest.raises(PaperlessUnavailableError, match="search unavailable"):
        await network.search_documents("synthetic")
    with pytest.raises(PaperlessUnavailableError, match="document unavailable"):
        await network.get_document(7)
    with pytest.raises(PaperlessUnavailableError, match="task unavailable"):
        await network.get_task(uuid4())
    with pytest.raises(PaperlessUnavailableError, match="note unavailable"):
        await network.add_note(7, "synthetic")

    server_error = _gateway(settings_factory, lambda _: httpx.Response(500))
    with pytest.raises(PaperlessUnavailableError, match="request failed"):
        await server_error.search_documents("synthetic")


@pytest.mark.asyncio
async def test_malformed_collection_and_taxonomy_responses(
    settings_factory: Callable[..., Settings],
) -> None:
    malformed_page = _gateway(
        settings_factory,
        lambda _: httpx.Response(200, json={"results": "not-a-list", "next": None}),
    )
    with pytest.raises(PaperlessUnavailableError, match="paginated"):
        await malformed_page.get_taxonomy()

    malformed_search = _gateway(
        settings_factory,
        lambda _: httpx.Response(200, json={"results": "not-a-list"}),
    )
    with pytest.raises(PaperlessUnavailableError, match="search"):
        await malformed_search.search_documents("synthetic")

    invalid_taxonomy = _gateway(
        settings_factory,
        lambda _: httpx.Response(
            200,
            json={"results": [{"id": "bad", "name": 9}], "next": None},
        ),
    )
    with pytest.raises(PaperlessUnavailableError, match="taxonomy"):
        await invalid_taxonomy.get_taxonomy()

    missing_results = _gateway(
        settings_factory,
        lambda _: httpx.Response(200, json={}),
    )
    with pytest.raises(PaperlessUnavailableError, match="paginated"):
        await missing_results.get_taxonomy()

    invalid_document = _gateway(
        settings_factory,
        lambda _: httpx.Response(200, text="not-json"),
    )
    with pytest.raises(PaperlessUnavailableError, match="document"):
        await invalid_document.get_document(7)


@pytest.mark.asyncio
async def test_archived_download_omits_original_parameter(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"archived",
            headers={"content-type": "application/pdf"},
        )

    download = await _gateway(settings_factory, handler).download(
        7,
        tmp_path / "archived",
        archived=True,
    )

    assert "original" not in requests[0].url.params
    assert download.path.read_bytes() == b"archived"


@pytest.mark.asyncio
async def test_write_and_download_filesystem_failures(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    gateway = _gateway(
        settings_factory,
        lambda _: httpx.Response(
            200,
            content=b"synthetic",
            headers={"content-type": "application/pdf"},
        ),
    )
    with pytest.raises(AmbiguousSubmissionError, match="outcome is unknown"):
        await gateway.submit_document(
            tmp_path / "missing",
            "synthetic.pdf",
            "application/pdf",
            MetadataGuidance(),
        )
    with pytest.raises(PaperlessUnavailableError, match="staging unavailable"):
        await gateway.download(7, tmp_path / "missing-parent" / "destination")


@pytest.mark.asyncio
async def test_validate_token_and_custom_token_headers(
    settings_factory: Callable[..., Settings],
) -> None:
    last_auth_header = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal last_auth_header
        last_auth_header = request.headers.get("authorization", "")
        if last_auth_header == "Token token-valid":
            return httpx.Response(200, json={"results": [], "count": 0})
        return httpx.Response(401, json={"detail": "Invalid token."})

    gateway = _gateway(settings_factory, handler)

    valid = await gateway.validate_token(SecretStr("token-valid"))
    assert valid is True
    assert last_auth_header == "Token token-valid"

    invalid = await gateway.validate_token(SecretStr("token-invalid"))
    assert invalid is False

    # Exception handling in validate_token
    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    failing_gateway = _gateway(settings_factory, failing_handler)
    assert await failing_gateway.validate_token(SecretStr("token-valid")) is False

    # Operational call passing custom token
    await gateway.chat("hello", token=SecretStr("token-valid"))
    assert last_auth_header == "Token token-valid"


@pytest.mark.asyncio
async def test_get_ai_suggestions(settings_factory: Callable[..., Settings]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents/7/ai_suggestions/":
            assert request.extensions["timeout"]["read"] == 150.0
            return httpx.Response(
                200,
                json={
                    "title": "Suggested Title",
                    "correspondents": [1],
                    "document_types": [],
                    "storage_paths": [5],
                    "tags": [2, 3],
                    "dates": ["2026-07-28", "not-a-date"],
                    "suggested_correspondents": ["New Person"],
                    "suggested_document_types": ["New Type"],
                    "suggested_storage_paths": ["New/Path"],
                    "suggested_tags": ["New Tag"],
                },
            )
        if request.url.path == "/api/documents/8/ai_suggestions/":
            return httpx.Response(400, text="AI is required for this feature")
        if request.url.path == "/api/documents/9/ai_suggestions/":
            return httpx.Response(400, json={"ai": ["Invalid AI configuration."]})
        if "10" in request.url.path:
            raise httpx.ConnectError("connection refused")
        if request.url.path == "/api/documents/11/ai_suggestions/":
            return httpx.Response(503, json={"ai": ["AI backend request timed out."]})
        if request.url.path == "/api/documents/12/ai_suggestions/":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(404)

    gateway = _gateway(settings_factory, handler)
    suggestions = await gateway.get_ai_suggestions(7)
    assert suggestions.title == "Suggested Title"
    assert suggestions.correspondent_ids == (1,)
    assert suggestions.document_type_ids == ()
    assert suggestions.storage_path_ids == (5,)
    assert suggestions.tag_ids == (2, 3)
    assert suggestions.dates == (
        SuggestedDate("2026-07-28", date(2026, 7, 28)),
        SuggestedDate("not-a-date", None),
    )
    assert suggestions.suggested_tags == ("New Tag",)
    assert suggestions.suggested_correspondents == ("New Person",)

    with pytest.raises(PaperlessAIDisabledError, match="disabled"):
        await gateway.get_ai_suggestions(8)
    with pytest.raises(PaperlessAIConfigurationError, match="configuration"):
        await gateway.get_ai_suggestions(9)

    with pytest.raises(PaperlessAITransportError, match="transport"):
        await gateway.get_ai_suggestions(10)
    with pytest.raises(PaperlessAITimeoutError, match="backend timed out"):
        await gateway.get_ai_suggestions(11)
    with pytest.raises(PaperlessAITimeoutError, match="timed out"):
        await gateway.get_ai_suggestions(12)


@pytest.mark.asyncio
async def test_update_document(settings_factory: Callable[..., Settings]) -> None:
    patch_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal patch_payload
        if request.url.path == "/api/documents/7/":
            patch_payload = json.loads(request.content)
            return httpx.Response(200, json={})
        if request.url.path == "/api/documents/10/":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(404)

    gateway = _gateway(settings_factory, handler)

    # Should not send request if updates are empty
    await gateway.update_document(7, DocumentUpdate())
    assert not patch_payload

    updates = DocumentUpdate(
        title="New",
        tag_ids=(4, 5),
        correspondent_id=1,
        document_type_id=2,
        storage_path_id=3,
        created=date(2026, 7, 28),
    )
    await gateway.update_document(7, updates)
    assert patch_payload == {
        "title": "New",
        "tags": [4, 5],
        "correspondent": 1,
        "document_type": 2,
        "storage_path": 3,
        "created": "2026-07-28",
    }

    with pytest.raises(PaperlessUnavailableError, match="request failed"):
        await gateway.update_document(8, updates)

    with pytest.raises(PaperlessUnavailableError, match="unavailable"):
        await gateway.update_document(10, updates)


@pytest.mark.asyncio
async def test_modify_document_tags_uses_bulk_edit(
    settings_factory: Callable[..., Settings],
) -> None:
    payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal payload
        assert request.url.path == "/api/documents/bulk_edit/"
        payload = json.loads(request.content)
        return httpx.Response(200, json={})

    gateway = _gateway(settings_factory, handler)
    await gateway.modify_document_tags(7, add_tag_ids=())
    assert payload == {}

    await gateway.modify_document_tags(7, add_tag_ids=(2, 3), remove_tag_ids=(4,))
    assert payload == {
        "documents": [7],
        "method": "modify_tags",
        "parameters": {"add_tags": [2, 3], "remove_tags": [4]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, PaperlessAuthenticationError),
        (403, PaperlessPermissionError),
        (500, AmbiguousPaperlessMutationError),
    ],
)
async def test_modify_document_tags_classifies_definite_and_ambiguous_failures(
    settings_factory: Callable[..., Settings],
    status_code: int,
    expected_error: type[PaperlessUnavailableError],
) -> None:
    gateway = _gateway(
        settings_factory,
        lambda _: httpx.Response(status_code, text="sensitive upstream detail"),
    )

    with pytest.raises(expected_error):
        await gateway.modify_document_tags(7, add_tag_ids=(2,), remove_tag_ids=(3,))


@pytest.mark.asyncio
async def test_new_taxonomy_endpoint_failures_are_fail_closed(
    settings_factory: Callable[..., Settings],
) -> None:
    def transport_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic", request=request)

    network = _gateway(settings_factory, transport_error)
    with pytest.raises(PaperlessUnavailableError, match="capabilities unavailable"):
        await network.get_taxonomy_capabilities()
    with pytest.raises(PaperlessUnavailableError, match="lookup unavailable"):
        await network.find_taxonomy_items(TaxonomyKind.TAG, "Synthetic")
    with pytest.raises(PaperlessUnavailableError, match="creation unavailable"):
        await network.create_taxonomy_item(TaxonomyKind.TAG, "Synthetic")
    with pytest.raises(PaperlessUnavailableError, match="document unavailable"):
        await network.get_document_tag_ids(7)
    with pytest.raises(PaperlessUnavailableError, match="documents unavailable"):
        await network.get_documents_tag_ids((7,))
    with pytest.raises(PaperlessUnavailableError, match="outcome is unknown"):
        await network.modify_document_tags(7, add_tag_ids=(1,))

    for capability_payload in ({}, {"permissions": "bad"}, {"permissions": [1]}):
        malformed = _gateway(settings_factory, _json_response(capability_payload))
        with pytest.raises(PaperlessUnavailableError, match="capabilities"):
            await malformed.get_taxonomy_capabilities()

    for lookup_payload in (
        {},
        {"results": "bad"},
        {"results": ["bad"]},
        {"results": [{"id": "bad", "name": 1}]},
    ):
        malformed = _gateway(settings_factory, _json_response(lookup_payload))
        with pytest.raises(PaperlessUnavailableError, match="lookup"):
            await malformed.find_taxonomy_items(TaxonomyKind.TAG, "Synthetic")

    for creation_response in (
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"id": "bad", "name": 1}),
    ):
        malformed = _gateway(settings_factory, _fixed_response(creation_response))
        with pytest.raises(PaperlessUnavailableError, match="taxonomy creation"):
            await malformed.create_taxonomy_item(TaxonomyKind.TAG, "Synthetic")

    for document_response in (
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
    ):
        malformed = _gateway(settings_factory, _fixed_response(document_response))
        with pytest.raises(PaperlessUnavailableError, match="document"):
            await malformed.get_document_tag_ids(7)

    for batch_payload in (
        {},
        {"results": "bad"},
        {"results": ["bad"]},
        {"results": [{"id": 999, "tags": []}]},
    ):
        malformed = _gateway(settings_factory, _json_response(batch_payload))
        with pytest.raises(PaperlessUnavailableError, match="document"):
            await malformed.get_documents_tag_ids((7,))


@pytest.mark.asyncio
async def test_ai_suggestion_malformed_and_generic_failures(
    settings_factory: Callable[..., Settings],
) -> None:
    responses = (
        httpx.Response(400, text="different bad request"),
        httpx.Response(503, text="not-json"),
        httpx.Response(503, json={"detail": "proxy unavailable"}),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"title": 7}),
    )
    for response in responses:
        gateway = _gateway(settings_factory, _fixed_response(response))
        with pytest.raises(PaperlessUnavailableError):
            await gateway.get_ai_suggestions(7)
