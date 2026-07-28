"""Sanitized application exceptions safe to map to Discord responses."""

from __future__ import annotations


class AssistantError(Exception):
    """Base class whose details must remain internal unless explicitly safe."""


class ConfigurationUnavailableError(AssistantError):
    """A required downstream object or capability is not ready."""


class PaperlessUnavailableError(AssistantError):
    """Paperless could not complete a bounded operation."""


class PaperlessAuthenticationError(PaperlessUnavailableError):
    """The configured Paperless credential was rejected."""


class PaperlessPermissionError(PaperlessUnavailableError):
    """The Paperless principal lacks permission for the requested operation."""


class PaperlessAIUnavailableError(PaperlessUnavailableError):
    """Base class for a diagnosable Paperless AI-suggestion failure."""


class PaperlessAIDisabledError(PaperlessAIUnavailableError):
    """Paperless has AI features disabled."""


class PaperlessAIConfigurationError(PaperlessAIUnavailableError):
    """Paperless rejected its configured AI backend or model settings."""


class PaperlessAITimeoutError(PaperlessAIUnavailableError):
    """The Paperless AI request exceeded a configured timeout."""


class PaperlessAITransportError(PaperlessAIUnavailableError):
    """The assistant could not complete transport to Paperless's AI endpoint."""


class AmbiguousSubmissionError(AssistantError):
    """The upload response is unknown and must not be retried automatically."""


class InvalidAttachmentError(AssistantError):
    """An attachment failed a user-correctable validation rule."""

    def __init__(self, user_message: str) -> None:
        super().__init__("attachment validation failed")
        self.user_message = user_message


class RateLimitedError(AssistantError):
    """A user exceeded the bounded native-chat rate."""


class ContextUnavailableError(AssistantError):
    """Conversational document context is absent or ambiguous."""


class UnlinkedUserError(AssistantError):
    """The Discord user has not linked their Paperless account yet."""


class StaleSuggestionError(AssistantError):
    """The document changed after the suggestion review was rendered."""


class InvalidTokenError(AssistantError):
    """The provided Paperless API token was rejected by Paperless."""
