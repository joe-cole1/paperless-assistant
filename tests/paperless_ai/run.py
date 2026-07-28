"""Exercise Paperless 3.0.4 AI suggestions, caching, invalidation, and writeback."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.models import (
    AISuggestions,
    DocumentUpdate,
    MetadataGuidance,
    TaskState,
    TaxonomyItem,
    TaxonomyKind,
)
from paperless_assistant.paperless import HttpPaperlessGateway

_EXPECTED_TITLE = "Synthetic Chat Conversation with Mary"
_MARKER = "SYNTHETIC PAPERLESS AI CACHE TEST"


def _settings(base_url: str) -> Settings:
    return Settings(
        _env_file=None,
        discord_token="synthetic",  # noqa: S106
        discord_guild_id=1,
        discord_questions_channel_id=2,
        discord_uploads_channel_id=3,
        discord_allowed_user_ids=frozenset({4}),
        encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        paperless_internal_url=base_url,
        paperless_public_url="https://paperless.example.invalid",
        paperless_token="unused-system-token",  # noqa: S106
        data_dir=Path(tempfile.gettempdir()),
    )


def _synthetic_pdf() -> bytes:
    text = f"{_MARKER} Mary conversation dated 2026-07-28"
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode())
        document.extend(value)
        document.extend(b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(document)


async def _wait_for_paperless(client: httpx.AsyncClient) -> None:
    for _ in range(120):
        try:
            response = await client.get("api/")
            if response.status_code < 500:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1)
    raise AssertionError("Paperless did not become ready")


@dataclass(frozen=True)
class Fixture:
    document_id: int
    tag: TaxonomyItem
    correspondent: TaxonomyItem
    document_type: TaxonomyItem
    storage_path: TaxonomyItem


async def _bootstrap_token(base_url: str) -> SecretStr:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as bootstrap:
        await _wait_for_paperless(bootstrap)
        token_response = await bootstrap.post(
            "api/token/",
            data={
                "username": "integration",
                "password": "synthetic-integration-password",
            },
        )
        token_response.raise_for_status()
        return SecretStr(token_response.json()["token"])


async def _seed_document(gateway: HttpPaperlessGateway, token: SecretStr) -> Fixture:
    capabilities = await gateway.get_taxonomy_capabilities(token=token)
    assert capabilities.add_tags
    assert capabilities.add_correspondents
    assert capabilities.add_document_types
    assert capabilities.add_storage_paths
    tag = await gateway.create_taxonomy_item(
        TaxonomyKind.TAG,
        "conversation",
        token=token,
    )
    correspondent = await gateway.create_taxonomy_item(
        TaxonomyKind.CORRESPONDENT,
        "Mary",
        token=token,
    )
    document_type = await gateway.create_taxonomy_item(
        TaxonomyKind.DOCUMENT_TYPE,
        "Chat Log",
        token=token,
    )
    storage_path = await gateway.create_taxonomy_item(
        TaxonomyKind.STORAGE_PATH,
        "Personal/Chat Logs",
        storage_path="Personal/Chat Logs",
        token=token,
    )
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "synthetic-cache-test.pdf"
        source.write_bytes(_synthetic_pdf())
        task_id = await gateway.submit_document(
            source,
            source.name,
            "application/pdf",
            MetadataGuidance(),
            token=token,
        )
        for _ in range(180):
            task = await gateway.get_task(task_id, token=token)
            if task.state in {TaskState.SUCCESS, TaskState.FAILURE}:
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError("Synthetic document consumption timed out")
    assert task.state is TaskState.SUCCESS
    assert task.document_id is not None
    return Fixture(int(task.document_id), tag, correspondent, document_type, storage_path)


async def _stats(client: httpx.AsyncClient) -> dict[str, object]:
    response = await client.get("http://127.0.0.1:18080/stats")
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


async def _exercise_cache(
    gateway: HttpPaperlessGateway,
    token: SecretStr,
    fixture: Fixture,
) -> AISuggestions:
    async with httpx.AsyncClient(timeout=5) as stats_client:
        assert (await _stats(stats_client))["request_count"] == 0
        first = await gateway.get_ai_suggestions(fixture.document_id, token=token)
        first_stats = await _stats(stats_client)
        assert first_stats["request_count"] == 1
        assert first_stats["saw_synthetic_marker"] is True
        assert first_stats["saw_tool_definition"] is True

        cached = await gateway.get_ai_suggestions(fixture.document_id, token=token)
        assert (await _stats(stats_client))["request_count"] == 1
        assert cached == first

        assert first.title == _EXPECTED_TITLE
        assert first.tag_ids == (fixture.tag.id,)
        assert first.correspondent_ids == (fixture.correspondent.id,)
        assert first.document_type_ids == (fixture.document_type.id,)
        assert first.storage_path_ids == (fixture.storage_path.id,)
        assert first.suggested_tags == ("new-topic",)
        assert first.suggested_correspondents == ("Abby",)
        assert first.suggested_document_types == ("Text Message",)
        assert first.suggested_storage_paths == ("Messages/Mary",)
        assert tuple(value.value for value in first.dates) == (date(2026, 7, 28),)

        await gateway.update_document(
            fixture.document_id,
            DocumentUpdate(title="Synthetic cache invalidation"),
            token=token,
        )
        invalidated = await gateway.get_ai_suggestions(fixture.document_id, token=token)
        assert (await _stats(stats_client))["request_count"] == 2
        assert invalidated == first
        return invalidated


async def _apply_and_verify(
    gateway: HttpPaperlessGateway,
    token: SecretStr,
    fixture: Fixture,
    suggestions: AISuggestions,
) -> None:
    await gateway.update_document(
        fixture.document_id,
        DocumentUpdate(
            title=suggestions.title,
            correspondent_id=suggestions.correspondent_ids[0],
            document_type_id=suggestions.document_type_ids[0],
            storage_path_id=suggestions.storage_path_ids[0],
            created=suggestions.dates[0].value,
        ),
        token=token,
    )
    await gateway.modify_document_tags(
        fixture.document_id,
        add_tag_ids=suggestions.tag_ids,
        token=token,
    )
    applied = await gateway.get_document(fixture.document_id, token=token)
    assert applied.title == _EXPECTED_TITLE
    assert applied.created == date(2026, 7, 28)
    assert applied.correspondent_id == fixture.correspondent.id
    assert applied.document_type_id == fixture.document_type.id
    assert applied.storage_path_id == fixture.storage_path.id
    assert fixture.tag.id in applied.tag_ids


async def main() -> None:
    port = int(os.environ.get("PAPERLESS_AI_TEST_PORT", "18765"))
    base_url = f"http://127.0.0.1:{port}/"
    token = await _bootstrap_token(base_url)
    gateway = HttpPaperlessGateway(_settings(base_url))
    try:
        fixture = await _seed_document(gateway, token)
        suggestions = await _exercise_cache(gateway, token, fixture)
        await _apply_and_verify(
            gateway,
            token,
            fixture,
            suggestions,
        )
    finally:
        await gateway.close()

    print(  # noqa: T201
        json.dumps(
            {
                "paperless_version": "3.0.4",
                "llm_requests": 2,
                "cache_hit_proven": True,
                "cache_invalidation_proven": True,
                "writeback_verified": True,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
