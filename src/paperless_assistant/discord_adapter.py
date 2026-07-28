"""Outbound Discord Gateway adapter for questions, delivery, and ingestion."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Coroutine, Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from uuid import uuid4

import discord
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    InvalidAttachmentError,
    PaperlessUnavailableError,
    RateLimitedError,
    StaleSuggestionError,
    UnlinkedUserError,
)
from paperless_assistant.models import (
    AISuggestions,
    DiscordMessageTarget,
    Document,
    DocumentId,
    DocumentUpdate,
    IngestionJob,
    JobState,
    ReferenceContext,
    SuggestionReview,
    SuggestionSelection,
    Taxonomy,
    TaxonomyItem,
    TaxonomyKind,
)
from paperless_assistant.policy import discord_safe_chunks, select_ordinal
from paperless_assistant.ports import CredentialRepository, PaperlessGateway
from paperless_assistant.services import (
    DeliveryService,
    IngestionOutcome,
    IngestionService,
    QueryResponse,
    QueryService,
    TaxonomyCache,
)

logger = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


def _is_delivery_request(content: str) -> bool:
    normalized = content.casefold()
    return any(word in normalized for word in ("send", "attach", "download", "give me"))


def _is_follow_up(content: str) -> bool:
    normalized = content.strip().casefold()
    prefixes = (
        "what about ",
        "when was ",
        "where was ",
        "who ",
        "why ",
        "how ",
        "does it ",
        "did it ",
        "is it ",
        "was it ",
        "summarize it",
        "tell me more",
    )
    return normalized.startswith(prefixes)


def _document_embed(document: Document, public_url: str) -> discord.Embed:
    created = document.created.strftime("%b %d, %Y") if document.created else "Unavailable"
    embed = discord.Embed(
        title=f"📄 {document.title}",
        url=public_url,
        color=discord.Color.blue(),
    )
    embed.add_field(name="Document Date", value=created, inline=True)
    embed.add_field(name="Paperless ID", value=str(int(document.id)), inline=True)
    return embed


class DismissButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        super().__init__(
            label="Dismiss",
            style=discord.ButtonStyle.secondary,
            emoji="🗑️",
            custom_id=f"paperless:dismiss:{uuid4().hex[:8]}",
        )
        self._allowed_user_ids = allowed_user_ids

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._allowed_user_ids and interaction.user.id not in self._allowed_user_ids:
            await interaction.response.send_message(
                "You are not authorized to dismiss this message.",
                ephemeral=True,
            )
            return
        if interaction.message is not None:
            with suppress(discord.HTTPException):
                await interaction.message.delete()


def _upload_outcome_view(
    allowed_user_ids: frozenset[int], public_url: str | None = None
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    if public_url is not None:
        view.add_item(
            discord.ui.Button(
                label="Open",
                style=discord.ButtonStyle.link,
                url=public_url,
            )
        )
    view.add_item(DismissButton(allowed_user_ids))
    return view


def _result_view(
    principal_id: int,
    document_id: int,
    public_url: str,
    allowed_user_ids: frozenset[int],
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="Open in Paperless",
            style=discord.ButtonStyle.link,
            url=public_url,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="Send File",
            style=discord.ButtonStyle.primary,
            custom_id=f"paperless:send:{principal_id}:{document_id}",
        )
    )
    view.add_item(DismissButton(allowed_user_ids))
    return view


class AISuggestionsTitleModal(discord.ui.Modal, title="Edit Document Title"):
    def __init__(self, review_view: AISuggestionsView) -> None:
        super().__init__()
        self.review_view = review_view
        self.title_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="Document title (blank keeps current)",
            default=review_view.selection.title or "",
            placeholder=review_view.document.title[:100],
            max_length=128,
            required=False,
        )
        self.add_item(self.title_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.review_view.job.principal_id:
            await interaction.response.send_message(
                "Only the uploader can edit this review.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        self.review_view.selection = replace(
            self.review_view.selection,
            title=self.title_input.value.strip() or None,
        )
        await interaction.response.defer()
        await self.review_view.render()


class AISuggestionsDateModal(discord.ui.Modal, title="Enter a Custom Document Date"):
    def __init__(self, review_view: AISuggestionsView) -> None:
        super().__init__()
        self.review_view = review_view
        self.date_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="Document date YYYY-MM-DD",
            placeholder=(
                review_view.document.created.isoformat()
                if review_view.document.created
                else "YYYY-MM-DD"
            ),
            max_length=10,
            required=True,
        )
        self.add_item(self.date_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.review_view.job.principal_id:
            await interaction.response.send_message(
                "Only the uploader can edit this review.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        raw_date = self.date_input.value.strip()
        try:
            selected_date = date.fromisoformat(raw_date)
        except ValueError:
            await interaction.response.send_message(
                "Use a valid date in YYYY-MM-DD format.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        self.review_view.selection = replace(
            self.review_view.selection,
            created=selected_date,
        )
        await interaction.response.defer()
        await self.review_view.render()


_TAXONOMY_LABELS = {
    TaxonomyKind.TAG: "Tags",
    TaxonomyKind.CORRESPONDENT: "Correspondent",
    TaxonomyKind.DOCUMENT_TYPE: "Document Type",
    TaxonomyKind.STORAGE_PATH: "Storage Path",
}
_NORMALIZE_TAXONOMY = re.compile(r"[^\w\s]")
_CLOSE_MATCH_THRESHOLD = 0.6
_SELECT_OPTION_LIMIT = 25


def _edit_kind_enabled(settings: Settings, kind: TaxonomyKind) -> bool:
    return {
        TaxonomyKind.TAG: settings.allow_edit_tags,
        TaxonomyKind.CORRESPONDENT: settings.allow_edit_correspondent,
        TaxonomyKind.DOCUMENT_TYPE: settings.allow_edit_document_type,
        TaxonomyKind.STORAGE_PATH: settings.allow_edit_storage_path,
    }[kind]


def _taxonomy_values(taxonomy: Taxonomy, kind: TaxonomyKind) -> tuple[TaxonomyItem, ...]:
    return {
        TaxonomyKind.TAG: taxonomy.tags,
        TaxonomyKind.CORRESPONDENT: taxonomy.correspondents,
        TaxonomyKind.DOCUMENT_TYPE: taxonomy.document_types,
        TaxonomyKind.STORAGE_PATH: taxonomy.storage_paths,
    }[kind]


def _suggested_names(suggestions: AISuggestions, kind: TaxonomyKind) -> tuple[str, ...]:
    return {
        TaxonomyKind.TAG: suggestions.suggested_tags,
        TaxonomyKind.CORRESPONDENT: suggestions.suggested_correspondents,
        TaxonomyKind.DOCUMENT_TYPE: suggestions.suggested_document_types,
        TaxonomyKind.STORAGE_PATH: suggestions.suggested_storage_paths,
    }[kind]


def _matched_ids(suggestions: AISuggestions, kind: TaxonomyKind) -> tuple[int, ...]:
    return {
        TaxonomyKind.TAG: suggestions.tag_ids,
        TaxonomyKind.CORRESPONDENT: suggestions.correspondent_ids,
        TaxonomyKind.DOCUMENT_TYPE: suggestions.document_type_ids,
        TaxonomyKind.STORAGE_PATH: suggestions.storage_path_ids,
    }[kind]


def _taxonomy_name(identifier: int, values: Sequence[TaxonomyItem]) -> str:
    return next((item.name for item in values if item.id == identifier), f"ID {identifier}")


def _normalized_taxonomy(value: str) -> str:
    return _NORMALIZE_TAXONOMY.sub("", value.casefold()).strip()


def _close_existing_items(
    names: Sequence[str],
    values: Sequence[TaxonomyItem],
    excluded_ids: Sequence[int],
) -> tuple[tuple[TaxonomyItem, str], ...]:
    candidates: list[tuple[TaxonomyItem, str, float]] = []
    excluded = frozenset(excluded_ids)
    for suggested in names:
        target = _normalized_taxonomy(suggested)
        ranked = sorted(
            (
                (
                    item,
                    SequenceMatcher(
                        None,
                        target,
                        _normalized_taxonomy(item.name),
                    ).ratio(),
                )
                for item in values
                if item.id not in excluded
            ),
            key=lambda value: value[1],
            reverse=True,
        )
        if ranked and ranked[0][1] >= _CLOSE_MATCH_THRESHOLD:
            candidates.append((ranked[0][0], suggested, ranked[0][1]))
    unique: dict[int, tuple[TaxonomyItem, str, float]] = {}
    for item, suggested, score in candidates:
        current = unique.get(item.id)
        if current is None or score > current[2]:
            unique[item.id] = (item, suggested, score)
    return tuple((item, suggested) for item, suggested, _ in unique.values())


def _bounded_lines(lines: Sequence[str]) -> str:
    if not lines:
        return "None"
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= 1024 else f"{rendered[:1019]}…"


class _DateSelect(discord.ui.Select[discord.ui.View]):
    def __init__(self, review_view: AISuggestionsView, row: int) -> None:
        self.review_view = review_view
        current = (
            review_view.document.created.isoformat() if review_view.document.created else "None"
        )
        options = [
            discord.SelectOption(
                label=f"Date · Keep current · {current}"[:100],
                value="keep",
                default=review_view.selection.created is None,
            )
        ]
        seen: set[date] = set()
        for suggestion in review_view.suggestions.dates:
            if suggestion.value is None or suggestion.value in seen:
                continue
            seen.add(suggestion.value)
            options.append(
                discord.SelectOption(
                    label=f"Date · Paperless suggestion · {suggestion.value.isoformat()}"[:100],
                    value=f"date:{suggestion.value.isoformat()}",
                    default=review_view.selection.created == suggestion.value,
                )
            )
        options.append(
            discord.SelectOption(
                label="Date · Enter a custom date…",
                value="custom",
            )
        )
        bounded_options = (
            options
            if len(options) <= _SELECT_OPTION_LIMIT
            else [options[0], *options[1 : _SELECT_OPTION_LIMIT - 1], options[-1]]
        )
        super().__init__(
            placeholder=review_view.date_placeholder()[:150],
            min_values=1,
            max_values=1,
            options=bounded_options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0]
        if selected == "custom":
            await interaction.response.send_modal(AISuggestionsDateModal(self.review_view))
            return
        self.review_view.selection = replace(
            self.review_view.selection,
            created=(
                None if selected == "keep" else date.fromisoformat(selected.removeprefix("date:"))
            ),
        )
        await interaction.response.defer()
        await self.review_view.render()


class _MetadataSelect(discord.ui.Select[discord.ui.View]):
    def __init__(self, review_view: AISuggestionsView, kind: TaxonomyKind, row: int) -> None:
        self.review_view = review_view
        self.kind = kind
        options = review_view.options_for(kind)
        if len(options) > _SELECT_OPTION_LIMIT:
            option_count = len(options)
            options = options[: _SELECT_OPTION_LIMIT - 1]
            options.append(
                discord.SelectOption(
                    label=f"Review all {option_count} choices…",
                    value="more",
                    description="Open a paginated selector for this category.",
                )
            )
        super().__init__(
            placeholder=review_view.metadata_placeholder(kind)[:150],
            min_values=0,
            max_values=len(options) if kind is TaxonomyKind.TAG else 1,
            options=options,
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if "more" in self.values:
            await interaction.response.send_message(
                f"Review {_TAXONOMY_LABELS[self.kind].lower()} on one or more pages.",
                view=_MetadataOverflowView(
                    self.review_view,
                    self.kind,
                ),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        self.review_view.update_selection(self.kind, tuple(self.values))
        await interaction.response.defer()
        await self.review_view.render()


class _MetadataPageSelect(discord.ui.Select[discord.ui.View]):
    def __init__(self, overflow: _MetadataOverflowView) -> None:
        self.overflow = overflow
        options = overflow.page_options
        super().__init__(
            placeholder=(
                f"{_TAXONOMY_LABELS[overflow.kind]} {overflow.page + 1}/{overflow.page_count}"
            ),
            min_values=0,
            max_values=len(options) if overflow.kind is TaxonomyKind.TAG else 1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.overflow.parent.update_selection(
            self.overflow.kind,
            tuple(self.values),
            preserve_unmentioned=self.overflow.kind is TaxonomyKind.TAG,
            visible_values=tuple(option.value for option in self.options),
        )
        await interaction.response.edit_message(view=self.overflow)
        await self.overflow.parent.render()


class _MetadataOverflowView(discord.ui.View):
    def __init__(
        self,
        parent: AISuggestionsView,
        kind: TaxonomyKind,
        *,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=300)
        self.parent = parent
        self.kind = kind
        self.page = page
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.parent.job.principal_id:
            return True
        await interaction.response.send_message(
            "Only the uploader can edit this review.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return False

    @property
    def page_count(self) -> int:
        return max(
            1,
            (len(self.parent.options_for(self.kind)) + _SELECT_OPTION_LIMIT - 1)
            // _SELECT_OPTION_LIMIT,
        )

    @property
    def page_options(self) -> list[discord.SelectOption]:
        start = self.page * _SELECT_OPTION_LIMIT
        return self.parent.options_for(self.kind)[start : start + _SELECT_OPTION_LIMIT]

    def _rebuild(self) -> None:
        for child in tuple(self.children):
            if isinstance(child, _MetadataPageSelect):
                self.remove_item(child)
        self.add_item(_MetadataPageSelect(self))
        self.previous_button.disabled = self.page == 0
        self.next_button.disabled = self.page + 1 >= self.page_count

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        self.page -= 1
        self._rebuild()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        self.page += 1
        self._rebuild()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Done", style=discord.ButtonStyle.primary, row=1)
    async def done_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await interaction.response.edit_message(content="Selection saved.", view=None)


class _ConfirmTaxonomyCreationView(discord.ui.View):
    def __init__(self, parent: AISuggestionsView) -> None:
        super().__init__(timeout=300)
        self.parent = parent

    @discord.ui.button(
        label="Confirm Create & Apply",
        style=discord.ButtonStyle.danger,
        emoji="⚠️",
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        if interaction.user.id != self.parent.job.principal_id:
            await interaction.response.send_message(
                "Only the uploader can confirm taxonomy creation.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.parent.apply(interaction, confirm_create=True)

    @discord.ui.button(label="Go Back", style=discord.ButtonStyle.secondary)
    async def back(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await interaction.response.edit_message(
            content="No taxonomy objects were created.",
            view=None,
        )


class AISuggestionsView(discord.ui.View):
    def __init__(
        self,
        job: IngestionJob,
        review: SuggestionReview,
        ingestion: IngestionService,
        settings: Settings,
    ) -> None:
        super().__init__(timeout=settings.suggestion_review_timeout_seconds)
        self.clear_items()
        self.job = job
        self.review = review
        self.ingestion = ingestion
        self.settings = settings
        self.selection = self._editable_initial_selection(review)
        self.initial_selection = self.selection
        self.saved_selection = SuggestionSelection()
        self._apply_lock = asyncio.Lock()
        self._applied = False
        self.title_message: discord.Message | None = None
        self.metadata_message: discord.Message | None = None
        self.actions_message: discord.Message | None = None
        self._rebuild_metadata_selects()

    def _editable_initial_selection(self, review: SuggestionReview) -> SuggestionSelection:
        initial = self.ingestion.initial_suggestion_selection(review)
        return replace(
            initial,
            title=initial.title if self.settings.allow_edit_title else None,
            created=initial.created if self.settings.allow_edit_date else None,
            correspondent_id=(
                initial.correspondent_id if self.settings.allow_edit_correspondent else None
            ),
            document_type_id=(
                initial.document_type_id if self.settings.allow_edit_document_type else None
            ),
            storage_path_id=(
                initial.storage_path_id if self.settings.allow_edit_storage_path else None
            ),
            tag_ids=initial.tag_ids if self.settings.allow_edit_tags else (),
        )

    @property
    def has_editable_fields(self) -> bool:
        return any(
            (
                self.settings.allow_edit_title,
                self.settings.allow_edit_date,
                self.settings.allow_edit_correspondent,
                self.settings.allow_edit_document_type,
                self.settings.allow_edit_storage_path,
                self.settings.allow_edit_tags,
            )
        )

    @property
    def is_dirty(self) -> bool:
        return self.selection != self.saved_selection

    def _rebuild_metadata_selects(self) -> None:
        self.clear_items()
        row = 0
        if self.settings.allow_edit_date:
            self.add_item(_DateSelect(self, row))
            row += 1
        for kind in (
            TaxonomyKind.CORRESPONDENT,
            TaxonomyKind.DOCUMENT_TYPE,
            TaxonomyKind.STORAGE_PATH,
            TaxonomyKind.TAG,
        ):
            if _edit_kind_enabled(self.settings, kind):
                self.add_item(_MetadataSelect(self, kind, row))
                row += 1

    async def send(self, thread: discord.Thread) -> None:
        if self.settings.allow_edit_title:
            self.title_message = await thread.send(
                self.title_content(),
                view=_TitleEditView(self),
                allowed_mentions=NO_MENTIONS,
            )
        if any(
            (
                self.settings.allow_edit_date,
                self.settings.allow_edit_correspondent,
                self.settings.allow_edit_document_type,
                self.settings.allow_edit_storage_path,
                self.settings.allow_edit_tags,
            )
        ):
            self.metadata_message = await thread.send(
                self.metadata_content(),
                view=self,
                allowed_mentions=NO_MENTIONS,
            )
        if self.has_editable_fields:
            self.actions_message = await thread.send(
                self.actions_content(),
                view=_ReviewActionsView(self),
                allowed_mentions=NO_MENTIONS,
            )

    async def render(self) -> None:
        self._rebuild_metadata_selects()
        if self.title_message is not None:
            await self.title_message.edit(
                content=self.title_content(),
                view=_TitleEditView(self),
                allowed_mentions=NO_MENTIONS,
            )
        if self.metadata_message is not None:
            await self.metadata_message.edit(
                content=self.metadata_content(),
                view=self,
                allowed_mentions=NO_MENTIONS,
            )
        if self.actions_message is not None:
            await self.actions_message.edit(
                content=self.actions_content(),
                view=_ReviewActionsView(self),
                allowed_mentions=NO_MENTIONS,
            )

    def title_content(self) -> str:
        value = self.selection.title or self.document.title
        state = "pending" if self.selection.title is not None else "current"
        return f"**Title**\n{value} *({state})*"

    @staticmethod
    def metadata_content() -> str:
        return (
            "**Editable Metadata**\n"
            "Each menu identifies its field. Changes remain pending until **Apply Changes**."
        )

    def actions_content(self) -> str:
        if self._applied:
            return (
                "**Changes applied**\n"
                "Paperless confirmed the selected metadata. Use **Refresh** to start a new review."
            )
        if self.is_dirty:
            return "**Pending changes**\nNothing is written until you choose **Apply Changes**."
        return "**No pending changes**"

    def date_placeholder(self) -> str:
        if self.selection.created is None:
            current = self.document.created.isoformat() if self.document.created else "None"
            return f"Date: Keep current ({current})"
        candidates = {value.value for value in self.suggestions.dates if value.value is not None}
        source = "Paperless suggestion" if self.selection.created in candidates else "custom"
        return f"Date: {self.selection.created.isoformat()} ({source})"

    def metadata_placeholder(self, kind: TaxonomyKind) -> str:
        if kind is TaxonomyKind.TAG:
            count = len(self.selection.tag_ids) + len(self.selection.new_tags)
            return f"Tags: {count} selected" if count else "Tags: Keep current"
        identifier = {
            TaxonomyKind.CORRESPONDENT: self.selection.correspondent_id,
            TaxonomyKind.DOCUMENT_TYPE: self.selection.document_type_id,
            TaxonomyKind.STORAGE_PATH: self.selection.storage_path_id,
        }[kind]
        names = {
            TaxonomyKind.CORRESPONDENT: self.selection.new_correspondents,
            TaxonomyKind.DOCUMENT_TYPE: self.selection.new_document_types,
            TaxonomyKind.STORAGE_PATH: self.selection.new_storage_paths,
        }[kind]
        label = _TAXONOMY_LABELS[kind]
        if names:
            return f"{label}: {names[0]} (new)"
        if identifier is not None:
            value = _taxonomy_name(
                identifier,
                _taxonomy_values(self.review.taxonomy, kind),
            )
            return f"{label}: {value}"
        return f"{label}: Keep current"

    @property
    def document(self) -> Document:
        return self.review.document

    @property
    def suggestions(self) -> AISuggestions:
        return self.review.suggestions

    @property
    def current_title(self) -> str:
        return self.selection.title or self.document.title

    def options_for(self, kind: TaxonomyKind) -> list[discord.SelectOption]:
        values = _taxonomy_values(self.review.taxonomy, kind)
        matched_ids = _matched_ids(self.suggestions, kind)
        suggestions = _suggested_names(self.suggestions, kind)
        options: list[discord.SelectOption] = []
        selected_id = {
            TaxonomyKind.CORRESPONDENT: self.selection.correspondent_id,
            TaxonomyKind.DOCUMENT_TYPE: self.selection.document_type_id,
            TaxonomyKind.STORAGE_PATH: self.selection.storage_path_id,
        }.get(kind)
        selected_new = {
            TaxonomyKind.TAG: self.selection.new_tags,
            TaxonomyKind.CORRESPONDENT: self.selection.new_correspondents,
            TaxonomyKind.DOCUMENT_TYPE: self.selection.new_document_types,
            TaxonomyKind.STORAGE_PATH: self.selection.new_storage_paths,
        }[kind]
        label = _TAXONOMY_LABELS[kind]
        options.append(
            discord.SelectOption(
                label=f"{label} · Keep current",
                value="keep",
                default=(
                    not self.selection.tag_ids and not selected_new
                    if kind is TaxonomyKind.TAG
                    else selected_id is None and not selected_new
                ),
            )
        )
        options.extend(
            [
                discord.SelectOption(
                    label=(f"{label} · Existing · {_taxonomy_name(identifier, values)}")[:100],
                    value=f"id:{identifier}",
                    description="Paperless matched this existing object.",
                    default=(
                        identifier in self.selection.tag_ids
                        if kind is TaxonomyKind.TAG
                        else selected_id == identifier
                    ),
                )
                for identifier in matched_ids
            ]
        )
        for item, suggested in _close_existing_items(suggestions, values, matched_ids):
            options.append(
                discord.SelectOption(
                    label=f"{label} · Close existing · {item.name}"[:100],
                    value=f"id:{item.id}",
                    description=f"Close to AI suggestion: {suggested}"[:100],
                    default=(
                        item.id in self.selection.tag_ids
                        if kind is TaxonomyKind.TAG
                        else selected_id == item.id
                    ),
                )
            )
        for index, name in enumerate(suggestions):
            options.append(
                discord.SelectOption(
                    label=f"{label} · New · {name}"[:100],
                    value=f"new:{index}",
                    description="Unchecked; selecting may require confirmed creation.",
                    default=name in selected_new,
                )
            )
        return options

    def update_selection(
        self,
        kind: TaxonomyKind,
        values: tuple[str, ...],
        *,
        preserve_unmentioned: bool = False,
        visible_values: tuple[str, ...] = (),
    ) -> None:
        if kind is TaxonomyKind.TAG and preserve_unmentioned:
            retained = tuple(
                value for value in self._selected_option_values(kind) if value not in visible_values
            )
            values = (*retained, *values)
        selected_ids = tuple(
            int(value.removeprefix("id:")) for value in values if value.startswith("id:")
        )
        names = _suggested_names(self.suggestions, kind)
        selected_names = tuple(
            names[int(value.removeprefix("new:"))] for value in values if value.startswith("new:")
        )
        if kind is TaxonomyKind.TAG:
            self.selection = replace(
                self.selection,
                tag_ids=selected_ids,
                new_tags=selected_names,
            )
        elif kind is TaxonomyKind.CORRESPONDENT:
            self.selection = replace(
                self.selection,
                correspondent_id=selected_ids[0] if selected_ids else None,
                new_correspondents=selected_names[:1],
            )
        elif kind is TaxonomyKind.DOCUMENT_TYPE:
            self.selection = replace(
                self.selection,
                document_type_id=selected_ids[0] if selected_ids else None,
                new_document_types=selected_names[:1],
            )
        else:
            self.selection = replace(
                self.selection,
                storage_path_id=selected_ids[0] if selected_ids else None,
                new_storage_paths=selected_names[:1],
            )
        self._rebuild_metadata_selects()

    def _selected_option_values(self, kind: TaxonomyKind) -> tuple[str, ...]:
        if kind is not TaxonomyKind.TAG:
            selected_id = {
                TaxonomyKind.CORRESPONDENT: self.selection.correspondent_id,
                TaxonomyKind.DOCUMENT_TYPE: self.selection.document_type_id,
                TaxonomyKind.STORAGE_PATH: self.selection.storage_path_id,
            }[kind]
            if selected_id is not None:
                return (f"id:{selected_id}",)
            names = {
                TaxonomyKind.CORRESPONDENT: self.selection.new_correspondents,
                TaxonomyKind.DOCUMENT_TYPE: self.selection.new_document_types,
                TaxonomyKind.STORAGE_PATH: self.selection.new_storage_paths,
            }[kind]
            suggested = _suggested_names(self.suggestions, kind)
            return tuple(f"new:{suggested.index(name)}" for name in names if name in suggested) or (
                "keep",
            )
        suggested_tags = self.suggestions.suggested_tags
        return (
            *(f"id:{identifier}" for identifier in self.selection.tag_ids),
            *(
                f"new:{suggested_tags.index(name)}"
                for name in self.selection.new_tags
                if name in suggested_tags
            ),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Allow only the uploader to operate this document review."""
        if interaction.user.id == self.job.principal_id:
            return True
        await interaction.response.send_message(
            "Only the user who uploaded this document can review its suggestions.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return False

    def _new_taxonomy_summary(self) -> tuple[str, ...]:
        return (
            *(f"Tag: {name}" for name in self.selection.new_tags),
            *(f"Correspondent: {name}" for name in self.selection.new_correspondents),
            *(f"Document type: {name}" for name in self.selection.new_document_types),
            *(f"Storage path: {name}" for name in self.selection.new_storage_paths),
        )

    async def _resolve_new_selection(self) -> SuggestionSelection:
        tag_ids = list(self.selection.tag_ids)
        for name in self.selection.new_tags:
            item = await self.ingestion.resolve_or_create_taxonomy(
                self.job,
                TaxonomyKind.TAG,
                name,
                confirm_create=True,
            )
            tag_ids.append(item.id)
        correspondent_id = self.selection.correspondent_id
        if self.selection.new_correspondents:
            item = await self.ingestion.resolve_or_create_taxonomy(
                self.job,
                TaxonomyKind.CORRESPONDENT,
                self.selection.new_correspondents[0],
                confirm_create=True,
            )
            correspondent_id = item.id
        document_type_id = self.selection.document_type_id
        if self.selection.new_document_types:
            item = await self.ingestion.resolve_or_create_taxonomy(
                self.job,
                TaxonomyKind.DOCUMENT_TYPE,
                self.selection.new_document_types[0],
                confirm_create=True,
            )
            document_type_id = item.id
        storage_path_id = self.selection.storage_path_id
        if self.selection.new_storage_paths:
            name = self.selection.new_storage_paths[0]
            item = await self.ingestion.resolve_or_create_taxonomy(
                self.job,
                TaxonomyKind.STORAGE_PATH,
                name,
                confirm_create=True,
                storage_path=name,
            )
            storage_path_id = item.id
        return replace(
            self.selection,
            correspondent_id=correspondent_id,
            document_type_id=document_type_id,
            storage_path_id=storage_path_id,
            tag_ids=tuple(dict.fromkeys(tag_ids)),
        )

    async def apply(
        self,
        interaction: discord.Interaction,
        *,
        confirm_create: bool,
    ) -> None:
        async with self._apply_lock:
            if self._applied:
                await interaction.followup.send(
                    "These selections were already applied.",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            selected = self.selection
            try:
                if confirm_create:
                    selected = await self._resolve_new_selection()
                updates = DocumentUpdate(
                    title=selected.title,
                    correspondent_id=selected.correspondent_id,
                    document_type_id=selected.document_type_id,
                    storage_path_id=selected.storage_path_id,
                    created=selected.created,
                    tag_ids=selected.tag_ids or None,
                )
                await self.ingestion.apply_suggestions(
                    self.job,
                    updates,
                    expected_modified=self.document.modified,
                )
                self.selection = selected
                self.saved_selection = selected
                self._applied = True
            except StaleSuggestionError:
                message = (
                    "The Paperless document changed after this review opened. "
                    "Refresh the review before applying."
                )
            except UnlinkedUserError:
                message = "Your Paperless account is no longer linked."
            except PaperlessUnavailableError:
                message = (
                    "Paperless could not resolve or apply these selections. "
                    "No automatic retry was attempted."
                )
            except Exception as error:
                logger.error(
                    "ai_suggestion_apply_failed",
                    extra={"error_type": type(error).__name__},
                )
                message = "The selections could not be applied. Please retry later."
            else:
                await self.render()
                await interaction.followup.send(
                    "Paperless confirmed the selected metadata.",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
                return
            await interaction.followup.send(
                message,
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )

    async def request_apply(self, interaction: discord.Interaction) -> None:
        new_items = self._new_taxonomy_summary()
        if new_items and self.settings.require_new_metadata_confirmation:
            await interaction.response.send_message(
                "The following selected names do not currently map to an exact Paperless object:\n"
                f"{_bounded_lines(tuple(f'• {value}' for value in new_items))}\n\n"
                "Paperless permissions and exact-name existence will be checked again before "
                "creation. Existing close matches remain preferred in the selection menus.",
                view=_ConfirmTaxonomyCreationView(self),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.apply(interaction, confirm_create=bool(new_items))

    async def reset(self, interaction: discord.Interaction) -> None:
        self.selection = self.initial_selection
        await interaction.response.defer(ephemeral=True)
        await self.render()
        await interaction.followup.send(
            "Pending choices were reset to Paperless's suggestions.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    async def reload(self) -> str | None:
        try:
            fresh = await self.ingestion.get_suggestion_review(self.job)
        except StaleSuggestionError:
            return "The document changed while Paperless generated suggestions. Try Refresh again."
        except UnlinkedUserError:
            return "Your Paperless account is no longer linked."
        except PaperlessUnavailableError:
            return "Paperless AI suggestions are unavailable. Check the server logs for details."
        if fresh is not None:
            self.review = fresh
            self.selection = self._editable_initial_selection(fresh)
            self.initial_selection = self.selection
            self.saved_selection = SuggestionSelection()
            self._applied = False
            await self.render()
        return None


class _TitleEditView(discord.ui.View):
    def __init__(self, parent: AISuggestionsView) -> None:
        super().__init__(timeout=parent.settings.suggestion_review_timeout_seconds)
        self.parent = parent
        self.edit_title.disabled = parent._applied

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    @discord.ui.button(label="Edit Title", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit_title(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await interaction.response.send_modal(AISuggestionsTitleModal(self.parent))


class _ReviewActionsView(discord.ui.View):
    def __init__(self, parent: AISuggestionsView) -> None:
        super().__init__(timeout=parent.settings.suggestion_review_timeout_seconds)
        self.parent = parent
        self.apply_changes.disabled = parent._applied
        self.reset_changes.disabled = parent._applied

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.parent.interaction_check(interaction)

    @discord.ui.button(label="Apply Changes", style=discord.ButtonStyle.green, emoji="✅")
    async def apply_changes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await self.parent.request_apply(interaction)

    @discord.ui.button(label="Reset Changes", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def reset_changes(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await self.parent.reset(interaction)


class _ReviewThreadController:
    def __init__(
        self,
        principal_id: int,
        timeout_seconds: float,
    ) -> None:
        self.principal_id = principal_id
        self.timeout_seconds = timeout_seconds
        self.sessions: list[AISuggestionsView] = []

    @property
    def is_dirty(self) -> bool:
        return any(session.is_dirty for session in self.sessions)

    def add(self, session: AISuggestionsView) -> None:
        self.sessions.append(session)

    def build_view(self, public_url: str | None) -> _ReviewThreadControlsView:
        return _ReviewThreadControlsView(self, public_url)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.principal_id:
            return True
        await interaction.response.send_message(
            "Only the uploader can control this document thread.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
        return False

    async def request_refresh(self, interaction: discord.Interaction) -> None:
        if self.is_dirty:
            await interaction.response.send_message(
                "Refreshing will discard unapplied changes. Continue?",
                view=_ConfirmRefreshView(self),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.refresh(interaction)

    async def refresh(self, interaction: discord.Interaction) -> None:
        errors: list[str] = []
        for session in self.sessions:
            error = await session.reload()
            if error is not None:
                errors.append(error)
        await interaction.followup.send(
            (
                "\n".join(errors)
                if errors
                else "Review refreshed. Paperless may have returned its cached AI response."
            ),
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    async def request_finish(self, interaction: discord.Interaction) -> None:
        if self.is_dirty:
            await interaction.response.send_message(
                "You have unapplied changes. Closing will discard them and delete this thread.",
                view=_ConfirmCloseThreadView(self),
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.finish(interaction)

    @staticmethod
    async def finish(interaction: discord.Interaction) -> None:
        delete = getattr(interaction.channel, "delete", None)
        if delete is None:
            await interaction.followup.send(
                "This control is only available inside an upload thread.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        try:
            await delete(reason="Uploader finished Paperless document review")
        except discord.HTTPException:
            await interaction.followup.send(
                "Discord could not delete this thread. Check the bot's Manage Threads permission.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )


class _ReviewThreadControlsView(discord.ui.View):
    def __init__(self, controller: _ReviewThreadController, public_url: str | None) -> None:
        super().__init__(timeout=controller.timeout_seconds)
        self.controller = controller
        if public_url is not None:
            self.add_item(
                discord.ui.Button(
                    label="Open Paperless",
                    style=discord.ButtonStyle.link,
                    url=public_url,
                )
            )
        self.add_item(_RefreshReviewButton(controller))
        self.add_item(_FinishReviewButton(controller))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.controller.interaction_check(interaction)


class _RefreshReviewButton(discord.ui.Button[_ReviewThreadControlsView]):
    def __init__(self, controller: _ReviewThreadController) -> None:
        super().__init__(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
        self.controller = controller

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.controller.request_refresh(interaction)


class _FinishReviewButton(discord.ui.Button[_ReviewThreadControlsView]):
    def __init__(self, controller: _ReviewThreadController) -> None:
        super().__init__(label="Finish & Close", style=discord.ButtonStyle.danger, emoji="🗑️")
        self.controller = controller

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.controller.request_finish(interaction)


class _ConfirmRefreshView(discord.ui.View):
    def __init__(self, controller: _ReviewThreadController) -> None:
        super().__init__(timeout=300)
        self.controller = controller

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.controller.interaction_check(interaction)

    @discord.ui.button(label="Discard & Refresh", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await interaction.response.defer(ephemeral=True)
        await self.controller.refresh(interaction)

    @discord.ui.button(label="Go Back", style=discord.ButtonStyle.secondary)
    async def back(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await interaction.response.edit_message(content="Refresh canceled.", view=None)


class _ConfirmCloseThreadView(discord.ui.View):
    def __init__(self, controller: _ReviewThreadController) -> None:
        super().__init__(timeout=300)
        self.controller = controller

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.controller.interaction_check(interaction)

    @discord.ui.button(label="Close Without Saving", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await interaction.response.defer(ephemeral=True)
        await self.controller.finish(interaction)

    @discord.ui.button(label="Go Back", style=discord.ButtonStyle.secondary)
    async def back(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[discord.ui.View],
    ) -> None:
        del button
        await interaction.response.edit_message(content="Thread left open.", view=None)


class DiscordAssistant(discord.Client):
    """Exact-allowlist Discord inbound adapter."""

    def __init__(  # noqa: PLR0913
        self,
        settings: Settings,
        query: QueryService,
        ingestion: IngestionService,
        delivery: DeliveryService,
        taxonomy: TaxonomyCache,
        *,
        credentials: CredentialRepository | None = None,
        paperless_gateway: PaperlessGateway | None = None,
        ready_callback: Callable[[bool], None],
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self._settings = settings
        self._query = query
        self._ingestion = ingestion
        self._delivery = delivery
        self._taxonomy = taxonomy
        self._credentials = credentials
        self._paperless_gateway = paperless_gateway
        self._ready_callback = ready_callback
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._pending_recovery: list[IngestionOutcome] = []
        self._staging_lock = asyncio.Lock()
        self.tree = discord.app_commands.CommandTree(self)
        self._register_commands()

    def _authorized_user_id(self, user_id: int) -> bool:
        return user_id in self._settings.discord_allowed_user_ids

    def _register_commands(self) -> None:
        @self.tree.command(
            name="clean",
            description="Clean up assistant messages in this channel.",
        )
        @discord.app_commands.describe(
            count="Number of recent channel messages to inspect and clean (default: 100)"
        )
        async def clean_command(interaction: discord.Interaction, count: int = 100) -> None:
            if not self._authorized_user_id(interaction.user.id):
                await interaction.response.send_message(
                    "You are not authorized to run this command.", ephemeral=True
                )
                return
            if interaction.channel_id not in (
                self._settings.discord_questions_channel_id,
                self._settings.discord_uploads_channel_id,
            ):
                await interaction.response.send_message(
                    "Clean command can only be used in assistant channels.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            channel = interaction.channel
            if not isinstance(channel, discord.TextChannel):
                await interaction.followup.send("Invalid channel type.", ephemeral=True)
                return
            cleaned = 0
            limit = min(max(1, count), 100)
            async for message in channel.history(limit=limit):
                if not message.pinned:
                    with suppress(discord.HTTPException):
                        await message.delete()
                        cleaned += 1
            await interaction.followup.send(f"Cleaned {cleaned} message(s).", ephemeral=True)

        self._register_auth_commands()

    def _register_auth_commands(self) -> None:
        auth_group = discord.app_commands.Group(
            name="auth",
            description="Paperless account authentication commands",
        )

        @auth_group.command(
            name="link",
            description="Securely link your Paperless API token.",
        )
        @discord.app_commands.describe(token="Your Paperless API token")  # noqa: S106
        async def auth_link(interaction: discord.Interaction, token: str) -> None:
            if not self._authorized_user_id(interaction.user.id):
                await interaction.response.send_message(
                    "You are not authorized to run this command.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            secret_token = SecretStr(token.strip())
            valid = False
            if self._paperless_gateway is not None:
                valid = await self._paperless_gateway.validate_token(secret_token)
            if not valid:
                await interaction.followup.send(
                    "❌ The provided Paperless API token was rejected by Paperless. "
                    "Please check your token and try again.",
                    ephemeral=True,
                )
                return
            if self._credentials is not None:
                await self._credentials.save_user_token(interaction.user.id, secret_token)
            await interaction.followup.send(
                "✅ Your Paperless API token has been securely linked!",
                ephemeral=True,
            )

        @auth_group.command(
            name="unlink",
            description="Revoke and remove your linked Paperless API token.",
        )
        async def auth_unlink(interaction: discord.Interaction) -> None:
            if not self._authorized_user_id(interaction.user.id):
                await interaction.response.send_message(
                    "You are not authorized to run this command.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            deleted = False
            if self._credentials is not None:
                deleted = await self._credentials.delete_user_token(interaction.user.id)
            if deleted:
                await interaction.followup.send(
                    "✅ Your linked Paperless token has been removed.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "No linked Paperless account token was found.", ephemeral=True
                )

        @auth_group.command(
            name="status",
            description="Check the status of your linked Paperless account.",
        )
        async def auth_status(interaction: discord.Interaction) -> None:
            if not self._authorized_user_id(interaction.user.id):
                await interaction.response.send_message(
                    "You are not authorized to run this command.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            user_token = (
                await self._credentials.get_user_token(interaction.user.id)
                if self._credentials is not None
                else None
            )
            if user_token is None:
                await interaction.followup.send(
                    "❌ You have not linked your Paperless account yet. "
                    "Use `/auth link <token>` to connect.",
                    ephemeral=True,
                )
                return
            valid = False
            if self._paperless_gateway is not None:
                valid = await self._paperless_gateway.validate_token(user_token)
            if valid:
                await interaction.followup.send(
                    "✅ Your Paperless account is linked and active.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "⚠️ Your linked Paperless token was rejected by Paperless (expired or revoked). "
                    "Please run `/auth link <token>` with a new token.",
                    ephemeral=True,
                )

        self.tree.add_command(auth_group)

    async def setup_hook(self) -> None:
        """Initialize downstream policy before accepting messages."""
        await self._taxonomy.refresh()
        await self._ingestion.recover(self._notify_recovery)
        self._start_background(self._taxonomy_loop())
        self._start_background(self._recovery_loop())
        with suppress(discord.HTTPException, discord.app_commands.MissingApplicationID):
            guild = discord.Object(id=self._settings.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            logger.info(
                "discord_commands_synced",
                extra={
                    "service": self._settings.app_name,
                    "guild_id": self._settings.discord_guild_id,
                },
            )
            await self.tree.sync(guild=guild)

    def _start_background(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def close(self) -> None:
        """Stop background policy loops and close the Gateway."""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await super().close()

    async def on_ready(self) -> None:
        """Publish safe readiness and the bounded missing-tag warning."""
        self._ready_callback(self._taxonomy.ingestion_ready)
        await self._flush_recovery_notifications()
        if self._taxonomy.ingestion_ready:
            await self._clear_missing_tag_warning()
        else:
            await self._warn_missing_tag()
        logger.info(
            "discord_ready",
            extra={"service": self._settings.app_name, "ready": self._taxonomy.ingestion_ready},
        )

    async def on_disconnect(self) -> None:
        """Discord availability is part of readiness, not liveness."""
        self._ready_callback(False)

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        """Report an unexpected event failure without logging private exception details."""
        del kwargs
        correlation_id = uuid4()
        logger.error(
            "discord_event_failed",
            extra={
                "service": self._settings.app_name,
                "event": event_method,
                "correlation_id": str(correlation_id),
            },
        )
        if event_method != "on_message" or not args:
            return
        message = args[0]
        if not self._authorized_message(message):
            return
        with suppress(discord.HTTPException):
            await message.reply(
                f"Something unexpected failed. Please retry. Reference `{correlation_id}`.",
                allowed_mentions=NO_MENTIONS,
            )

    def _authorized_message(self, message: discord.Message) -> bool:
        if not (
            message.guild is not None
            and message.guild.id == self._settings.discord_guild_id
            and self._authorized_user_id(message.author.id)
            and not message.author.bot
            and message.webhook_id is None
        ):
            return False
        if isinstance(message.channel, discord.Thread):
            return message.channel.parent_id in (
                self._settings.discord_questions_channel_id,
                self._settings.discord_uploads_channel_id,
            )
        return message.channel.id in (
            self._settings.discord_questions_channel_id,
            self._settings.discord_uploads_channel_id,
        )

    async def on_message(self, message: discord.Message) -> None:
        """Route only allowlisted messages in configured channels or their threads."""
        if not self._authorized_message(message):
            return
        channel_id = (
            message.channel.parent_id
            if isinstance(message.channel, discord.Thread)
            else message.channel.id
        )
        if channel_id == self._settings.discord_questions_channel_id:
            await self._questions_message(message)
        else:
            await self._uploads_message(message)

    async def _questions_message(self, message: discord.Message) -> None:  # noqa: PLR0911, PLR0912
        if message.attachments:
            await message.reply(
                f"Please upload documents in <#{self._settings.discord_uploads_channel_id}>.",
                allowed_mentions=NO_MENTIONS,
            )
            return
        question = message.content
        if not question.strip():
            return
        if isinstance(message.channel, discord.Thread):
            thread = message.channel
        else:
            clean_prompt = " ".join(question.strip().split())
            snippet = clean_prompt[:40] if clean_prompt else "Question"
            thread = await message.create_thread(
                name=f"Q: {snippet}",
                auto_archive_duration=1440,
            )
        context_id = thread.id
        context = await self._query.context(context_id)
        if context and _is_delivery_request(question):
            selected = select_ordinal(question, [int(item) for item in context.document_ids])
            if selected is not None:
                if not selected:
                    await thread.send(
                        "That result number is not available anymore.",
                        allowed_mentions=NO_MENTIONS,
                    )
                else:
                    await self._deliver_to_message(message, selected, target=thread)
                return

        should_continue, target_document = await self._reply_target(message, context)
        if not should_continue:
            return
        if target_document is None and context and _is_follow_up(question):
            if len(context.document_ids) == 1:
                target_document = int(context.document_ids[0])
            else:
                await thread.send(
                    "Which result do you mean—first, second, or third?",
                    allowed_mentions=NO_MENTIONS,
                )
                return
        status = await thread.send("Searching Paperless…", allowed_mentions=NO_MENTIONS)
        try:
            response = await self._query.ask(
                message.author.id,
                question,
                document_id=target_document,
                context_id=context_id,
            )
        except RateLimitedError:
            await status.edit(
                content=(
                    "You've asked several questions quickly. Please try again in a few minutes."
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        except UnlinkedUserError:
            await status.edit(
                content=(
                    "You have not linked your Paperless account yet. "
                    "Please run `/auth link <token>` to connect."
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        except PaperlessUnavailableError:
            await status.edit(
                content="Paperless is unavailable right now. Please try again shortly.",
                allowed_mentions=NO_MENTIONS,
            )
            return
        await self._render_query(status, response, message.author.id, context_id=context_id)

    async def _reply_target(
        self, message: discord.Message, context: ReferenceContext | None
    ) -> tuple[bool, int | None]:
        if (
            context is None
            or message.reference is None
            or message.reference.message_id not in context.source_message_ids
        ):
            return True, None
        index = context.source_message_ids.index(message.reference.message_id)
        if index < len(context.document_ids):
            return True, int(context.document_ids[index])
        return False, None

    async def _render_query(
        self,
        status: discord.Message,
        response: QueryResponse,
        principal_id: int,
        context_id: int | None = None,
    ) -> None:
        chunks = discord_safe_chunks(response.answer)
        first = chunks[0] if chunks else "Paperless returned no answer."
        target_context_id = context_id if context_id is not None else principal_id
        try:
            await status.edit(content=first, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            await status.channel.send(first, allowed_mentions=NO_MENTIONS)
        for chunk in chunks[1:]:
            await status.channel.send(chunk, allowed_mentions=NO_MENTIONS)
        result_message_ids: list[int] = []
        for document in response.documents:
            url = self._delivery_url(int(document.id))
            result_message = await status.channel.send(
                embed=_document_embed(document, url),
                view=_result_view(
                    principal_id, int(document.id), url, self._settings.discord_allowed_user_ids
                ),
                allowed_mentions=NO_MENTIONS,
            )
            result_message_ids.append(result_message.id)
        if response.documents:
            await self._query.save_rendered_context(
                target_context_id,
                tuple(document.id for document in response.documents),
                tuple(result_message_ids),
            )

    def _delivery_url(self, document_id: int) -> str:
        base = str(self._settings.paperless_public_url).rstrip("/")
        return f"{base}/documents/{document_id}/details"

    async def _deliver_to_message(
        self,
        message: discord.Message,
        document_ids: Sequence[int],
        *,
        target: discord.abc.Messageable | None = None,
    ) -> None:
        limit = self._attachment_limit(message.guild)
        for document_id in document_ids:
            try:
                plan = await self._delivery.prepare(message.author.id, document_id, limit)
                if plan.attachment is None:
                    content = (
                        "This file is too large for Discord. "
                        f"[Download original]({plan.original_url})"
                    )
                    if target is not None:
                        await target.send(content, allowed_mentions=NO_MENTIONS)
                    else:
                        await message.reply(content, allowed_mentions=NO_MENTIONS)
                else:
                    prefix = (
                        f"Archived PDF attached; [download original]({plan.original_url})."
                        if plan.used_archived
                        else "Here's the original file."
                    )
                    file = discord.File(
                        plan.attachment.path,
                        filename=plan.attachment.filename,
                    )
                    if target is not None:
                        await target.send(prefix, file=file, allowed_mentions=NO_MENTIONS)
                    else:
                        await message.reply(prefix, file=file, allowed_mentions=NO_MENTIONS)
            except PaperlessUnavailableError:
                content = "That document is unavailable right now."
                if target is not None:
                    await target.send(content, allowed_mentions=NO_MENTIONS)
                else:
                    await message.reply(content, allowed_mentions=NO_MENTIONS)
            finally:
                if "plan" in locals():
                    self._delivery.cleanup(plan)
                    del plan

    @staticmethod
    def _attachment_limit(guild: discord.Guild | None) -> int:
        return guild.filesize_limit if guild is not None else 10 * 1024 * 1024

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle restart-safe Send File component IDs through database context."""
        custom_id = (
            interaction.data.get("custom_id") if isinstance(interaction.data, dict) else None
        )
        if not isinstance(custom_id, str) or not custom_id.startswith("paperless:send:"):
            return
        try:
            _, _, principal_raw, document_raw = custom_id.split(":")
            principal_id = int(principal_raw)
            document_id = int(document_raw)
        except TypeError, ValueError:
            return
        channel = getattr(interaction, "channel", None)
        channel_id = (
            channel.parent_id if isinstance(channel, discord.Thread) else interaction.channel_id
        )
        authorized = bool(
            interaction.guild_id == self._settings.discord_guild_id
            and channel_id == self._settings.discord_questions_channel_id
            and interaction.user.id == principal_id
            and self._authorized_user_id(principal_id)
        )
        target_context_id = channel.id if isinstance(channel, discord.Thread) else principal_id
        context = await self._query.context(target_context_id) if authorized else None
        if context is None or DocumentId(document_id) not in context.document_ids:
            await interaction.response.send_message(
                "That file request has expired or is unavailable.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True)
        limit = getattr(interaction, "filesize_limit", self._attachment_limit(interaction.guild))
        try:
            plan = await self._delivery.prepare(principal_id, document_id, limit)
            if plan.attachment is None:
                await interaction.followup.send(
                    f"Too large for Discord. [Download original]({plan.original_url})",
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
            else:
                note = (
                    f"Archived PDF attached. [Download original]({plan.original_url})"
                    if plan.used_archived
                    else "Original file attached."
                )
                await interaction.followup.send(
                    note,
                    file=discord.File(plan.attachment.path, filename=plan.attachment.filename),
                    ephemeral=True,
                    allowed_mentions=NO_MENTIONS,
                )
        except UnlinkedUserError:
            await interaction.followup.send(
                "You have not linked your Paperless account yet. "
                "Please run `/auth link <token>` to connect.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        except PaperlessUnavailableError:
            await interaction.followup.send(
                "That document is unavailable right now.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
        finally:
            if "plan" in locals():
                self._delivery.cleanup(plan)

    async def _uploads_message(  # noqa: PLR0912, PLR0915
        self, message: discord.Message
    ) -> None:
        if not message.attachments:
            await message.reply(
                "Attach one or more documents here to upload them to Paperless.",
                allowed_mentions=NO_MENTIONS,
            )
            return
        if not self._taxonomy.ingestion_ready:
            await message.reply(
                f"Ingestion is paused until the exact Paperless tag "
                f"`{self._settings.paperless_source_tag}` exists and is unique.",
                allowed_mentions=NO_MENTIONS,
            )
            return
        if isinstance(message.channel, discord.Thread):
            thread = message.channel
        else:
            first_filename = message.attachments[0].filename
            snippet = first_filename[:40]
            thread = await message.create_thread(
                name=f"Upload: {snippet}",
                auto_archive_duration=1440,
            )
        status = await thread.send(
            f"Received {len(message.attachments)} file(s); validating…",
            allowed_mentions=NO_MENTIONS,
        )
        attachments = message.attachments[: self._settings.discord_max_attachments]
        results: list[str] = []
        jobs: list[tuple[int, IngestionJob]] = []
        review_controller = _ReviewThreadController(
            message.author.id,
            self._settings.suggestion_review_timeout_seconds,
        )
        all_resolved = len(message.attachments) <= self._settings.discord_max_attachments
        if len(message.attachments) > self._settings.discord_max_attachments:
            results.append(
                f"Only the first {self._settings.discord_max_attachments} files can be processed."
            )
        for index, attachment in enumerate(attachments, start=1):
            if attachment.size > self._settings.discord_max_attachment_bytes:
                results.append(f"{index}. `{attachment.filename}` — too large to ingest.")
                all_resolved = False
                continue
            staged_path = self._settings.staging_dir / str(uuid4())
            async with self._staging_lock:
                if (
                    self._staging_usage() + attachment.size
                    > self._settings.ingestion_max_staged_bytes
                ):
                    results.append(f"{index}. `{attachment.filename}` — staging quota exceeded.")
                    all_resolved = False
                    continue
                try:
                    await attachment.save(staged_path, use_cached=False)
                    staged_path.chmod(0o600)
                    actual_size = staged_path.stat().st_size
                    if actual_size > self._settings.discord_max_attachment_bytes:
                        raise InvalidAttachmentError(
                            "The downloaded file exceeds the ingestion limit."
                        )
                    if self._staging_usage() > self._settings.ingestion_max_staged_bytes:
                        raise InvalidAttachmentError("The staging quota was exceeded.")
                    job = await self._ingestion.stage(
                        discord_message_id=message.id,
                        discord_attachment_id=attachment.id,
                        discord_status_message_id=status.id,
                        principal_id=message.author.id,
                        staged_path=staged_path,
                        original_filename=attachment.filename,
                        caption=message.content,
                        discord_message_channel_id=message.channel.id,
                        discord_status_channel_id=thread.id,
                    )
                    if job is None:
                        staged_path.unlink(missing_ok=True)
                        results.append(f"{index}. `{attachment.filename}` — already received.")
                    else:
                        jobs.append((index, job))
                except InvalidAttachmentError as error:
                    staged_path.unlink(missing_ok=True)
                    results.append(f"{index}. `{attachment.filename}` — {error.user_message}")
                    all_resolved = False
                except UnlinkedUserError:
                    staged_path.unlink(missing_ok=True)
                    results.append(
                        f"{index}. `{attachment.filename}` — link your Paperless account first."
                    )
                    all_resolved = False
                except discord.HTTPException, OSError:
                    staged_path.unlink(missing_ok=True)
                    results.append(f"{index}. `{attachment.filename}` — download failed; retry it.")
                    all_resolved = False

        submitted: list[tuple[int, IngestionJob]] = []
        try:
            for index, job in jobs:
                outcome = await self._ingestion.submit(job)
                if outcome.job.state == JobState.SUBMITTED:
                    submitted.append((index, outcome.job))
                    results.append(f"{index}. `{job.original_filename}` — processing in Paperless…")
                elif outcome.job.state == JobState.RECONCILIATION_REQUIRED:
                    results.append(
                        f"{index}. `{job.original_filename}` — upload outcome is uncertain; "
                        f"reconcile job `{job.id}` in Paperless."
                    )
                    all_resolved = False
                else:
                    results.append(f"{index}. `{job.original_filename}` — Paperless rejected it.")
                    all_resolved = False
        except UnlinkedUserError:
            await status.edit(
                content=(
                    "You have not linked your Paperless account yet. "
                    "Please run `/auth link <token>` to connect."
                ),
                allowed_mentions=NO_MENTIONS,
            )
            return
        await self._replace_status(status, results)

        if submitted:
            outcomes = await asyncio.gather(
                *(self._ingestion.poll_until_notifiable(job) for _, job in submitted),
                return_exceptions=True,
            )
            for (index, job), recovered in zip(submitted, outcomes, strict=True):
                if isinstance(recovered, BaseException):
                    results.append(
                        f"{index}. `{job.original_filename}` — status unavailable; "
                        "recovery will keep checking."
                    )
                    all_resolved = False
                elif recovered.job.state == JobState.SUCCEEDED and recovered.document:
                    note = " (guidance note failed)" if recovered.note_failed else ""
                    results.append(
                        f"{index}. `{job.original_filename}` — uploaded as "
                        f"[{recovered.document.title}]"
                        f"({self._delivery_url(int(recovered.document.id))})"
                        f"{note}."
                    )
                    try:
                        review = await self._ingestion.get_suggestion_review(recovered.job)
                    except PaperlessUnavailableError, StaleSuggestionError:
                        review = None
                        results.append(
                            f"{index}. AI suggestions are unavailable; "
                            "the server log contains the Paperless error."
                        )
                    except UnlinkedUserError:
                        review = None
                        results.append(
                            f"{index}. AI suggestions are unavailable because "
                            "the Paperless account is no longer linked."
                        )
                    if review is not None:
                        session = await self._send_suggestions_ui(thread, recovered.job, review)
                        review_controller.add(session)
                elif recovered.notification_timed_out:
                    results.append(
                        f"{index}. `{job.original_filename}` — still processing; "
                        f"task `{job.paperless_task_id}` will keep being checked."
                    )
                    all_resolved = False
                else:
                    guidance = (
                        " Verify Paperless Tika/Gotenberg and the Office-upload flag."
                        if job.office_dependent
                        else ""
                    )
                    results.append(
                        f"{index}. `{job.original_filename}` — processing failed.{guidance}"
                    )
                    all_resolved = False
            succeeded_url = next(
                (
                    self._delivery_url(int(recovered.document.id))
                    for _, recovered in zip(submitted, outcomes, strict=True)
                    if not isinstance(recovered, BaseException)
                    and recovered.job.state == JobState.SUCCEEDED
                    and recovered.document is not None
                ),
                None,
            )
            await self._replace_status(
                status,
                results,
                succeeded_url,
                view=(
                    review_controller.build_view(succeeded_url)
                    if review_controller.sessions
                    else None
                ),
            )
        if all_resolved:
            with suppress(discord.HTTPException):
                await message.delete()

    async def _send_suggestions_ui(
        self,
        thread: discord.Thread,
        job: IngestionJob,
        review: SuggestionReview,
    ) -> AISuggestionsView:
        view = AISuggestionsView(
            job,
            review,
            self._ingestion,
            self._settings,
        )
        await view.send(thread)
        return view

    async def _replace_status(
        self,
        status: discord.Message,
        lines: Sequence[str],
        public_url: str | None = None,
        *,
        view: discord.ui.View | None = None,
    ) -> None:
        chunks = discord_safe_chunks("\n".join(lines))
        first = chunks[0] if chunks else "No files were processed."
        status_view = view or _upload_outcome_view(
            self._settings.discord_allowed_user_ids,
            public_url,
        )
        try:
            await status.edit(content=first, view=status_view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            await status.channel.send(first, view=status_view, allowed_mentions=NO_MENTIONS)
        for chunk in chunks[1:]:
            if view is not None:
                await status.channel.send(chunk, allowed_mentions=NO_MENTIONS)
            else:
                await status.channel.send(
                    chunk,
                    view=_upload_outcome_view(self._settings.discord_allowed_user_ids),
                    allowed_mentions=NO_MENTIONS,
                )

    def _staging_usage(self) -> int:
        try:
            return sum(
                path.stat().st_size
                for path in self._settings.staging_dir.iterdir()
                if path.is_file()
            )
        except OSError:
            return self._settings.ingestion_max_staged_bytes

    async def _taxonomy_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.paperless_taxonomy_refresh_seconds)
            ready = await self._taxonomy.refresh()
            self._ready_callback(ready and self.is_ready())
            if ready:
                await self._clear_missing_tag_warning()
            else:
                await self._warn_missing_tag()

    async def _recovery_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.paperless_task_recovery_interval_seconds)
            await self._ingestion.recover(self._notify_recovery)

    async def _notify_recovery(self, outcome: IngestionOutcome) -> None:
        channel = self.get_channel(self._settings.discord_uploads_channel_id)
        if not isinstance(channel, discord.TextChannel):
            if all(item.job.id != outcome.job.id for item in self._pending_recovery):
                self._pending_recovery.append(outcome)
            return
        state = outcome.job.state.value.replace("_", " ")
        public_url: str | None = None
        if outcome.job.state == JobState.SUCCEEDED and outcome.document is not None:
            note = " Guidance note failed." if outcome.note_failed else ""
            public_url = self._delivery_url(int(outcome.document.id))
            content = (
                f"Recovered ingestion job `{outcome.job.id}`: "
                f"[{outcome.document.title}]"
                f"({public_url}) succeeded.{note}"
            )
        elif outcome.job.state == JobState.RECONCILIATION_REQUIRED:
            content = (
                f"Recovered ingestion job `{outcome.job.id}`: upload outcome is uncertain. "
                "Check Paperless before retrying."
            )
        elif outcome.job.state == JobState.FAILED and outcome.job.office_dependent:
            content = (
                f"Recovered ingestion job `{outcome.job.id}`: processing failed. "
                "Verify Paperless Tika/Gotenberg and the Office-upload flag."
            )
        elif outcome.job.state == JobState.FAILED:
            content = f"Recovered ingestion job `{outcome.job.id}`: processing failed."
        else:
            content = f"Recovered ingestion job `{outcome.job.id}`: {state}."
        await channel.send(
            content,
            view=_upload_outcome_view(self._settings.discord_allowed_user_ids, public_url),
            allowed_mentions=NO_MENTIONS,
        )
        if outcome.job.state == JobState.SUCCEEDED and await self._ingestion.message_succeeded(
            outcome.job.discord_message_id
        ):
            source_channel_id = (
                outcome.job.discord_message_channel_id or self._settings.discord_uploads_channel_id
            )
            await self.cleanup_messages(
                (),
                (
                    DiscordMessageTarget(
                        channel_id=source_channel_id,
                        message_id=outcome.job.discord_message_id,
                    ),
                ),
            )

    async def _flush_recovery_notifications(self) -> None:
        pending = tuple(self._pending_recovery)
        self._pending_recovery.clear()
        for outcome in pending:
            await self._notify_recovery(outcome)

    async def cleanup_messages(
        self,
        question_targets: Sequence[DiscordMessageTarget],
        upload_targets: Sequence[DiscordMessageTarget],
    ) -> tuple[DiscordMessageTarget, ...]:
        """Delete recorded messages from their exact parent or thread channel."""
        confirmed: list[DiscordMessageTarget] = []
        grouped: dict[int, list[DiscordMessageTarget]] = {}
        for target in (*question_targets, *upload_targets):
            grouped.setdefault(target.channel_id, []).append(target)
        for channel_id, targets in grouped.items():
            channel = self.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel | discord.Thread):
                guild = self.get_guild(self._settings.discord_guild_id)
                if guild is None:
                    continue
                try:
                    fetched = await guild.fetch_channel(channel_id)
                except discord.NotFound:
                    confirmed.extend(targets)
                    continue
                except discord.HTTPException:
                    continue
                if not isinstance(fetched, discord.TextChannel | discord.Thread):
                    continue
                channel = fetched
            for target in targets:
                try:
                    await channel.get_partial_message(target.message_id).delete()
                except discord.NotFound:
                    confirmed.append(target)
                except discord.HTTPException:
                    continue
                else:
                    confirmed.append(target)
        return tuple(dict.fromkeys(confirmed))

    async def _warn_missing_tag(self) -> None:
        now = datetime.now(tz=UTC)
        previous = await self._ingestion.warning_state()
        if previous and now - previous[1] < timedelta(hours=24):
            return
        channel = self.get_channel(self._settings.discord_uploads_channel_id)
        if isinstance(channel, discord.TextChannel):
            warning = await channel.send(
                f"Uploads are paused: create one exact, unique Paperless tag named "
                f"`{self._settings.paperless_source_tag}`. Questions and downloads still work.",
                allowed_mentions=NO_MENTIONS,
            )
            await self._ingestion.record_warning(warning.id, now)
            if previous and previous[0] != warning.id:
                with suppress(discord.HTTPException):
                    await channel.get_partial_message(previous[0]).delete()

    async def _clear_missing_tag_warning(self) -> None:
        previous = await self._ingestion.warning_state()
        if previous is None:
            return
        channel = self.get_channel(self._settings.discord_uploads_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.get_partial_message(previous[0]).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return
        await self._ingestion.clear_warning()
