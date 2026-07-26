"""Contract tests for the narrow Paperless v3 HTTP adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

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
