"""Deterministic taxonomy, attachment, and Discord text policy tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from paperless_assistant.errors import InvalidAttachmentError
from paperless_assistant.models import Taxonomy, TaxonomyItem
from paperless_assistant.policy import (
    _looks_like_text,
    discord_safe_chunks,
    find_required_tag,
    normalize_text,
    resolve_taxonomy,
    select_ordinal,
    sum_sizes,
    validate_attachment,
)


def test_taxonomy_exact_matching_and_ambiguity() -> None:
    source = TaxonomyItem(1, "Discord")
    taxonomy = Taxonomy(
        tags=(
            source,
            TaxonomyItem(2, "Venice"),
            TaxonomyItem(3, "Travel"),
            TaxonomyItem(4, "travel"),
        ),
        correspondents=(TaxonomyItem(10, "John"), TaxonomyItem(11, "Clinic")),
        document_types=(TaxonomyItem(20, "Vaccine Record"), TaxonomyItem(21, "Receipt")),
    )

    guidance = resolve_taxonomy(
        "John's Vaccine Record from Venice; travel was lovely.",
        taxonomy,
        source,
    )

    assert guidance.tag_ids == (1, 2)
    assert guidance.correspondent_id == 10
    assert guidance.document_type_id == 20
    assert normalize_text("\uff36\uff25\uff2e\uff29\uff23\uff25") == "venice"


def test_multiple_single_value_matches_apply_neither() -> None:
    source = TaxonomyItem(1, "Discord")
    taxonomy = Taxonomy(
        tags=(source,),
        correspondents=(TaxonomyItem(2, "John"), TaxonomyItem(3, "Clinic")),
        document_types=(TaxonomyItem(4, "Receipt"), TaxonomyItem(5, "Invoice")),
    )
    guidance = resolve_taxonomy("John Clinic Receipt Invoice", taxonomy, source)

    assert guidance.correspondent_id is None
    assert guidance.document_type_id is None


def test_empty_taxonomy_names_never_match() -> None:
    source = TaxonomyItem(1, "Discord")
    taxonomy = Taxonomy((source, TaxonomyItem(2, "")), (), ())

    assert resolve_taxonomy("", taxonomy, source).tag_ids == (1,)


def test_required_tag_must_be_unique_and_exact() -> None:
    unique = Taxonomy((TaxonomyItem(1, "Discord"),), (), ())
    duplicate = Taxonomy((TaxonomyItem(1, "Discord"), TaxonomyItem(2, "discord")), (), ())

    assert find_required_tag(unique, "discord") == TaxonomyItem(1, "Discord")
    assert find_required_tag(duplicate, "Discord") is None
    assert find_required_tag(unique, "Disc") is None


@pytest.mark.parametrize(
    ("filename", "content", "media_type"),
    [
        ("a.pdf", b"%PDF-1.7 synthetic", "application/pdf"),
        ("a.png", b"\x89PNG\r\n\x1a\nsynthetic", "image/png"),
        ("a.jpg", b"\xff\xd8\xffsynthetic", "image/jpeg"),
        ("a.jpeg", b"\xff\xd8\xffsynthetic", "image/jpeg"),
        ("a.tif", b"II*\x00synthetic", "image/tiff"),
        ("a.tiff", b"MM\x00*synthetic", "image/tiff"),
        ("a.gif", b"GIF89asynthetic", "image/gif"),
        ("a.webp", b"RIFF\x00\x00\x00\x00WEBPsynthetic", "image/webp"),
        ("a.txt", b"Synthetic text", "text/plain"),
    ],
)
def test_native_signatures(tmp_path: Path, filename: str, content: bytes, media_type: str) -> None:
    path = tmp_path / "staged"
    path.write_bytes(content)

    assert validate_attachment(path, filename, office_enabled=False) == (media_type, False)


@pytest.mark.parametrize(
    ("filename", "member", "media"),
    [
        (
            "a.docx",
            "word/document.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "a.pptx",
            "ppt/presentation.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        (
            "a.xlsx",
            "xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
)
def test_ooxml_signatures(tmp_path: Path, filename: str, member: str, media: str) -> None:
    path = tmp_path / "staged"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr(member, "synthetic")

    assert validate_attachment(path, filename, office_enabled=True) == (media, True)


@pytest.mark.parametrize(
    ("filename", "mime", "media"),
    [
        (
            "a.odt",
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.text",
        ),
        (
            "a.odp",
            "application/vnd.oasis.opendocument.presentation",
            "application/vnd.oasis.opendocument.presentation",
        ),
        (
            "a.ods",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.spreadsheet",
        ),
    ],
)
def test_odf_signatures(tmp_path: Path, filename: str, mime: str, media: str) -> None:
    path = tmp_path / "staged"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", mime)

    assert validate_attachment(path, filename, office_enabled=True) == (media, True)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.doc", "application/msword"),
        ("a.ppt", "application/vnd.ms-powerpoint"),
        ("a.xls", "application/vnd.ms-excel"),
    ],
)
def test_legacy_office_signatures(tmp_path: Path, filename: str, expected: str) -> None:
    path = tmp_path / "staged"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1synthetic")

    assert validate_attachment(path, filename, office_enabled=True) == (expected, True)


def test_eml_signature(tmp_path: Path) -> None:
    path = tmp_path / "staged"
    path.write_text("From: synthetic@example.test\nSubject: Test\n\nBody")

    assert validate_attachment(path, "mail.eml", office_enabled=True) == (
        "message/rfc822",
        True,
    )


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("image.heic", b"synthetic", "Convert"),
        ("docs.zip", b"PK\x03\x04", "Archive"),
        ("unknown.bin", b"synthetic", "not supported"),
        ("fake.pdf", b"not a pdf", "does not match"),
        ("bad.txt", b"a\x00b", "UTF-8"),
        ("bad.eml", b"no email headers", "email signature"),
        ("file.doc", b"not ole", "does not match"),
    ],
)
def test_invalid_files_are_actionable(
    tmp_path: Path, filename: str, content: bytes, message: str
) -> None:
    path = tmp_path / "staged"
    path.write_bytes(content)

    with pytest.raises(InvalidAttachmentError, match="attachment validation failed") as caught:
        validate_attachment(path, filename, office_enabled=True)
    assert message in caught.value.user_message


def test_text_and_zip_faults_fail_closed(tmp_path: Path) -> None:
    assert not _looks_like_text(b"", tmp_path / "missing")

    invalid_utf8 = tmp_path / "invalid-utf8"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(InvalidAttachmentError) as invalid_text:
        validate_attachment(invalid_utf8, "invalid.txt", office_enabled=False)
    assert "UTF-8" in invalid_text.value.user_message

    malformed_zip = tmp_path / "malformed"
    malformed_zip.write_bytes(b"PK\x03\x04not-a-zip")
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(malformed_zip, "malformed.docx", office_enabled=True)

    unknown_ooxml = tmp_path / "unknown-ooxml"
    with zipfile.ZipFile(unknown_ooxml, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("custom/item.xml", "synthetic")
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(unknown_ooxml, "unknown.docx", office_enabled=True)

    oversized_mimetype = tmp_path / "oversized-mimetype"
    with zipfile.ZipFile(oversized_mimetype, "w") as archive:
        archive.writestr("mimetype", "x" * 257)
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(oversized_mimetype, "oversized.odt", office_enabled=True)

    invalid_mimetype = tmp_path / "invalid-mimetype"
    with zipfile.ZipFile(invalid_mimetype, "w") as archive:
        archive.writestr("mimetype", b"\xff")
    with pytest.raises(InvalidAttachmentError):
        validate_attachment(invalid_mimetype, "invalid.odt", office_enabled=True)


def test_office_disabled(tmp_path: Path) -> None:
    path = tmp_path / "staged"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    with pytest.raises(InvalidAttachmentError) as caught:
        validate_attachment(path, "a.doc", office_enabled=False)
    assert "disabled" in caught.value.user_message


def test_discord_chunks_mentions_and_ordinals() -> None:
    chunks = discord_safe_chunks("@everyone " + ("word " * 900), limit=100)

    assert len(chunks) > 2
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all("@everyone" not in chunk for chunk in chunks)
    assert discord_safe_chunks("") == ()
    assert select_ordinal("send me the second one", [11, 12, 13]) == (12,)
    assert select_ordinal("send all of them", [11, 12, 13]) == (11, 12, 13)
    assert select_ordinal("send the third", [11]) == ()
    assert select_ordinal("what did it say?", [11]) is None
    assert sum_sizes([1, 2, 3]) == 6


def test_discord_chunks_prefer_newlines_and_drop_trailing_whitespace() -> None:
    newline_chunks = discord_safe_chunks(("a" * 80) + "\n" + ("b" * 80), limit=100)
    exact_chunks = discord_safe_chunks(("x" * 100) + " ", limit=100)

    assert newline_chunks == ("a" * 80, "b" * 80)
    assert exact_chunks == ("x" * 100,)
