"""Deterministic format, taxonomy, routing, and Discord text policy."""

from __future__ import annotations

import re
import unicodedata
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from paperless_assistant.errors import InvalidAttachmentError
from paperless_assistant.models import MetadataGuidance, Taxonomy, TaxonomyItem

NATIVE_EXTENSIONS = frozenset(
    {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".webp", ".txt"}
)
OFFICE_EXTENSIONS = frozenset(
    {
        ".doc",
        ".docx",
        ".odt",
        ".ppt",
        ".pptx",
        ".odp",
        ".xls",
        ".xlsx",
        ".ods",
        ".eml",
    }
)
HEIC_EXTENSIONS = frozenset({".heic", ".heif"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"})


def normalize_text(value: str) -> str:
    """Normalize user and taxonomy text without linguistic inference."""
    return unicodedata.normalize("NFKC", value).casefold()


def _whole_phrase_present(caption: str, name: str) -> bool:
    normalized_caption = normalize_text(caption)
    normalized_name = normalize_text(name)
    if not normalized_name:
        return False
    pattern = rf"(?<!\w){re.escape(normalized_name)}(?!\w)"
    return re.search(pattern, normalized_caption, flags=re.UNICODE) is not None


def _matches(caption: str, items: Sequence[TaxonomyItem]) -> tuple[TaxonomyItem, ...]:
    duplicates: dict[str, int] = {}
    for item in items:
        key = normalize_text(item.name)
        duplicates[key] = duplicates.get(key, 0) + 1
    return tuple(
        item
        for item in items
        if duplicates[normalize_text(item.name)] == 1 and _whole_phrase_present(caption, item.name)
    )


def resolve_taxonomy(
    caption: str,
    taxonomy: Taxonomy,
    required_tag: TaxonomyItem,
) -> MetadataGuidance:
    """Resolve only exact, visible, unambiguous taxonomy names."""
    tags = _matches(caption, taxonomy.tags)
    correspondents = _matches(caption, taxonomy.correspondents)
    document_types = _matches(caption, taxonomy.document_types)
    tag_ids = {item.id for item in tags}
    tag_ids.add(required_tag.id)
    return MetadataGuidance(
        tag_ids=tuple(sorted(tag_ids)),
        correspondent_id=correspondents[0].id if len(correspondents) == 1 else None,
        document_type_id=document_types[0].id if len(document_types) == 1 else None,
    )


def find_required_tag(taxonomy: Taxonomy, configured_name: str) -> TaxonomyItem | None:
    """Return the exact required tag only when its visible name is unique."""
    normalized = normalize_text(configured_name)
    matches = tuple(item for item in taxonomy.tags if normalize_text(item.name) == normalized)
    return matches[0] if len(matches) == 1 else None


def _looks_like_text(header: bytes, path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            sample = stream.read(64 * 1024)
        if b"\x00" in sample:
            return False
        sample.decode("utf-8-sig")
    except OSError, UnicodeDecodeError:
        return False
    return True


def _zip_kind(path: Path) -> str | None:  # noqa: PLR0911
    try:
        with zipfile.ZipFile(path) as archive:
            names = frozenset(archive.namelist())
            if "[Content_Types].xml" in names:
                if any(name.startswith("word/") for name in names):
                    return ".docx"
                if any(name.startswith("ppt/") for name in names):
                    return ".pptx"
                if any(name.startswith("xl/") for name in names):
                    return ".xlsx"
            if "mimetype" in names:
                if archive.getinfo("mimetype").file_size > 256:
                    return None
                media = archive.read("mimetype").decode("ascii", errors="strict")
                return {
                    "application/vnd.oasis.opendocument.text": ".odt",
                    "application/vnd.oasis.opendocument.presentation": ".odp",
                    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
                }.get(media)
    except OSError, UnicodeDecodeError, zipfile.BadZipFile, KeyError:
        return None
    return None


def _detected_extension(path: Path, header: bytes) -> str | None:
    signatures = (
        ((b"%PDF-",), ".pdf"),
        ((b"\x89PNG\r\n\x1a\n",), ".png"),
        ((b"\xff\xd8\xff",), ".jpg"),
        ((b"II*\x00", b"MM\x00*"), ".tiff"),
        ((b"GIF87a", b"GIF89a"), ".gif"),
    )
    for prefixes, extension in signatures:
        if header.startswith(prefixes):
            return extension
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".ole"
    if header.startswith(b"PK\x03\x04"):
        return _zip_kind(path)
    return None


def validate_attachment(path: Path, filename: str, *, office_enabled: bool) -> tuple[str, bool]:
    """Validate extension and signature and return media type and Office dependency."""
    extension = Path(filename).suffix.casefold()
    if extension in HEIC_EXTENSIONS:
        raise InvalidAttachmentError("HEIC/HEIF is not supported. Convert it to JPEG or PDF.")
    if extension in ARCHIVE_EXTENSIONS:
        raise InvalidAttachmentError(
            "Archive files are not accepted. Upload each document directly."
        )
    supported = NATIVE_EXTENSIONS | OFFICE_EXTENSIONS
    if extension not in supported:
        raise InvalidAttachmentError("That file type is not supported.")
    if extension in OFFICE_EXTENSIONS and not office_enabled:
        raise InvalidAttachmentError("Office and email uploads are disabled on this deployment.")

    with path.open("rb") as stream:
        header = stream.read(64)
    detected = _detected_extension(path, header)

    if extension == ".txt":
        if not _looks_like_text(header, path):
            raise InvalidAttachmentError("The .txt file is not valid UTF-8 plain text.")
        return "text/plain", False
    if extension == ".eml":
        if not _looks_like_text(header, path) or b":" not in header:
            raise InvalidAttachmentError("The .eml file does not have a valid email signature.")
        return "message/rfc822", True
    if extension in {".doc", ".ppt", ".xls"}:
        if detected != ".ole":
            raise InvalidAttachmentError("The file extension does not match its content.")
        media = {
            ".doc": "application/msword",
            ".ppt": "application/vnd.ms-powerpoint",
            ".xls": "application/vnd.ms-excel",
        }[extension]
        return media, True

    aliases = {
        ".jpeg": ".jpg",
        ".tif": ".tiff",
    }
    if detected != aliases.get(extension, extension):
        raise InvalidAttachmentError("The file extension does not match its content.")
    media_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".tiff": "image/tiff",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".odt": "application/vnd.oasis.opendocument.text",
        ".odp": "application/vnd.oasis.opendocument.presentation",
        ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    }
    return media_types[aliases.get(extension, extension)], extension in OFFICE_EXTENSIONS


def discord_safe_chunks(text: str, *, limit: int = 1900) -> tuple[str, ...]:
    """Neutralize mentions and split without silently dropping answer text."""
    safe = text.replace("@", "@\u200b")
    if not safe:
        return ()
    chunks: list[str] = []
    remaining = safe
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def select_ordinal(value: str, document_ids: Sequence[int]) -> tuple[int, ...] | None:
    """Resolve simple conversational send-one/send-all requests deterministically."""
    normalized = normalize_text(value)
    if re.search(r"\b(all|everything|each)\b", normalized):
        return tuple(document_ids)
    ordinals = {
        "first": 0,
        "1st": 0,
        "second": 1,
        "2nd": 1,
        "third": 2,
        "3rd": 2,
        "one": 0,
        "two": 1,
        "three": 2,
    }
    for word, index in ordinals.items():
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized):
            return (document_ids[index],) if index < len(document_ids) else ()
    return None


def sum_sizes(values: Iterable[int]) -> int:
    """Return a bounded-batch byte total."""
    return sum(values)
