"""Outbound Discord Gateway adapter for questions, delivery, and ingestion."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import discord
from pydantic import SecretStr

from paperless_assistant.config import Settings
from paperless_assistant.errors import (
    InvalidAttachmentError,
    PaperlessUnavailableError,
    RateLimitedError,
    UnlinkedUserError,
)
from paperless_assistant.models import (
    AISuggestions,
    Document,
    DocumentId,
    DocumentUpdate,
    IngestionJob,
    JobState,
    ReferenceContext,
    Taxonomy,
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


class AISuggestionsEditModal(discord.ui.Modal, title="Edit Suggested Title"):
    def __init__(
        self,
        current_title: str,
        callback: Callable[[discord.Interaction, str], Coroutine[Any, Any, None]],
    ) -> None:
        super().__init__()
        self.callback = callback
        self.title_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="Document Title",
            default=current_title,
            max_length=128,
            required=True,
        )
        self.add_item(self.title_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.callback(interaction, self.title_input.value)


def _build_suggestions_embed(
    document: Document,
    suggestions: AISuggestions,
    taxonomy: Taxonomy | None,
    current_title: str | None = None,
) -> discord.Embed:
    title_val = current_title or suggestions.title or document.title or "None"
    embed = discord.Embed(
        title=f"🤖 AI Suggestions for {document.title}",
        description="Paperless AI has suggested the following metadata.",
        color=discord.Color.purple(),
    )
    embed.add_field(
        name="Suggested Title",
        value=title_val,
        inline=False,
    )

    corr_name = "None"
    if suggestions.correspondent_id and taxonomy:
        corr_name = next(
            (c.name for c in taxonomy.correspondents if c.id == suggestions.correspondent_id),
            str(suggestions.correspondent_id),
        )
    embed.add_field(name="Correspondent", value=corr_name, inline=True)

    type_name = "None"
    if suggestions.document_type_id and taxonomy:
        type_name = next(
            (t.name for t in taxonomy.document_types if t.id == suggestions.document_type_id),
            str(suggestions.document_type_id),
        )
    embed.add_field(name="Document Type", value=type_name, inline=True)

    tags_value = "None"
    if suggestions.tag_ids and taxonomy:
        names = [
            next((t.name for t in taxonomy.tags if t.id == tid), str(tid))
            for tid in suggestions.tag_ids
        ]
        tags_value = ", ".join(names)
    embed.add_field(name="Tags", value=tags_value, inline=False)

    return embed


class AISuggestionsView(discord.ui.View):
    def __init__(  # noqa: PLR0913, PLR0917
        self,
        job: IngestionJob,
        document: Document,
        suggestions: AISuggestions,
        ingestion: IngestionService,
        allowed_user_ids: frozenset[int],
        taxonomy_cache: TaxonomyCache | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.job = job
        self.document = document
        self.suggestions = suggestions
        self.ingestion = ingestion
        self.allowed_user_ids = allowed_user_ids
        self.taxonomy_cache = taxonomy_cache
        self.current_title = suggestions.title or document.title

    @discord.ui.button(label="Approve All & Apply", style=discord.ButtonStyle.green, emoji="🟢")
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if self.allowed_user_ids and interaction.user.id not in self.allowed_user_ids:
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()

        updates = DocumentUpdate(
            title=self.current_title,
            correspondent_id=self.suggestions.correspondent_id,
            document_type_id=self.suggestions.document_type_id,
            tag_ids=self.suggestions.tag_ids,
        )
        try:
            await self.ingestion.apply_suggestions(self.job, updates)
            if interaction.message:
                embed = interaction.message.embeds[0]
                embed.title = f"✅ Applied to {self.document.title}"
                embed.color = discord.Color.green()
                await interaction.message.edit(embed=embed, view=None)
        except Exception as e:
            await interaction.followup.send(f"Failed to apply: {e}", ephemeral=True)

    @discord.ui.button(label="Edit Title", style=discord.ButtonStyle.secondary, emoji="✏️")
    async def edit_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if self.allowed_user_ids and interaction.user.id not in self.allowed_user_ids:
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return

        async def on_modal_submit(modal_interaction: discord.Interaction, new_title: str) -> None:
            self.current_title = new_title
            if modal_interaction.message:
                embed = modal_interaction.message.embeds[0]
                for i, field in enumerate(embed.fields):
                    if field.name == "Suggested Title":
                        embed.set_field_at(i, name=field.name, value=new_title, inline=field.inline)
                        break
                await modal_interaction.response.edit_message(embed=embed, view=self)
            else:
                await modal_interaction.response.defer()

        await interaction.response.send_modal(
            AISuggestionsEditModal(self.current_title, on_modal_submit)
        )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if self.allowed_user_ids and interaction.user.id not in self.allowed_user_ids:
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        await interaction.response.defer()

        fresh = await self.ingestion.get_suggestions_for_job(self.job, max_attempts=1, delay=0.0)
        if fresh:
            self.suggestions = fresh
            self.current_title = fresh.title or self.document.title

        taxonomy = self.taxonomy_cache.snapshot if self.taxonomy_cache else None
        embed = _build_suggestions_embed(
            self.document, self.suggestions, taxonomy, current_title=self.current_title
        )
        if interaction.message:
            await interaction.message.edit(embed=embed, view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[discord.ui.View]
    ) -> None:
        if self.allowed_user_ids and interaction.user.id not in self.allowed_user_ids:
            await interaction.response.send_message("Unauthorized.", ephemeral=True)
            return
        if interaction.message:
            await interaction.message.delete()


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
        if not self._settings.discord_allowed_user_ids:
            return True
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
                    suggestions = await self._ingestion.get_suggestions_for_job(recovered.job)
                    if suggestions and (
                        suggestions.title
                        or suggestions.correspondent_id
                        or suggestions.document_type_id
                        or suggestions.tag_ids
                    ):
                        await self._send_suggestions_ui(
                            thread, recovered.job, recovered.document, suggestions
                        )
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
            await self._replace_status(status, results, succeeded_url)
        if all_resolved:
            with suppress(discord.HTTPException):
                await message.delete()

    async def _send_suggestions_ui(
        self,
        thread: discord.Thread,
        job: IngestionJob,
        document: Document,
        suggestions: AISuggestions,
    ) -> None:
        taxonomy = self._taxonomy.snapshot
        embed = _build_suggestions_embed(document, suggestions, taxonomy)
        view = AISuggestionsView(
            job,
            document,
            suggestions,
            self._ingestion,
            self._settings.discord_allowed_user_ids,
            taxonomy_cache=self._taxonomy,
        )
        await thread.send(embed=embed, view=view, allowed_mentions=NO_MENTIONS)

    async def _replace_status(
        self,
        status: discord.Message,
        lines: Sequence[str],
        public_url: str | None = None,
    ) -> None:
        chunks = discord_safe_chunks("\n".join(lines))
        first = chunks[0] if chunks else "No files were processed."
        view = _upload_outcome_view(self._settings.discord_allowed_user_ids, public_url)
        try:
            await status.edit(content=first, view=view, allowed_mentions=NO_MENTIONS)
        except discord.HTTPException:
            await status.channel.send(first, view=view, allowed_mentions=NO_MENTIONS)
        for chunk in chunks[1:]:
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
            with suppress(discord.HTTPException):
                await channel.get_partial_message(outcome.job.discord_message_id).delete()

    async def _flush_recovery_notifications(self) -> None:
        pending = tuple(self._pending_recovery)
        self._pending_recovery.clear()
        for outcome in pending:
            await self._notify_recovery(outcome)

    async def cleanup_messages(
        self,
        question_message_ids: Sequence[int],
        upload_message_ids: Sequence[int],
    ) -> None:
        """Delete only recorded old messages in their exact configured channels."""
        for channel_id, message_ids in (
            (self._settings.discord_questions_channel_id, question_message_ids),
            (self._settings.discord_uploads_channel_id, upload_message_ids),
        ):
            channel = self.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            for message_id in message_ids:
                with suppress(discord.HTTPException):
                    await channel.get_partial_message(message_id).delete()

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
