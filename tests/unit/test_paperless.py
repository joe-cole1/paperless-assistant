"""Contract tests for the narrow Paperless v3 HTTP adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    AmbiguousSubmissionError,
    PaperlessAuthenticationError,
    PaperlessUnavailableError,
)
from paperless_assistant.models import DocumentId, MetadataGuidance, TaskState
from paperless_assistant.paperless import (
    CHAT_METADATA_DELIMITER,
    HttpPaperlessGateway,
    _document,
    _parse_date,
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


def test_document_payload_validation_and_dates() -> None:
    assert _parse_date(None) is None
    assert _parse_date("") is None
    with pytest.raises(PaperlessUnavailableError, match="malformed document"):
        _document([])
    with pytest.raises(PaperlessUnavailableError, match="malformed document"):
        _document({"id": "7", "title": 8})


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
    assert await gateway.get_document_tag_ids(101) == ()
    assert await gateway.get_document_tag_ids(404) == ()
    assert await gateway.get_document_tag_ids(500) == ()


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
    assert len(calls) == 4


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
    assert first.document_id == DocumentId(9)
    assert second.state == TaskState.STARTED
    assert third.state == TaskState.UNKNOWN


@pytest.mark.asyncio
async def test_sanitized_error_boundaries(
    tmp_path: Path,
    settings_factory: Callable[..., Settings],
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"%PDF-1.7")

    auth = _gateway(settings_factory, lambda _: httpx.Response(401))
    with pytest.raises(PaperlessAuthenticationError):
        await auth.get_document(1)

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
    with pytest.raises(PaperlessUnavailableError, match="HTTP 500"):
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
