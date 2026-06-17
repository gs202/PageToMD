"""Typed exception hierarchy for :mod:`pagetomd`.

Every exception carries ``exit_code`` and ``hint`` attributes that the CLI
uses to produce stable, actionable error output. Exit codes are stable across
releases.
"""

from __future__ import annotations

__all__ = [
    "ConfigError",
    "ConversionError",
    "DependencyMissingError",
    "ExtractionEmptyError",
    "FetchError",
    "PageToMdError",
    "RobotsDisallowedError",
    "UsageError",
    "WriteError",
]


class PageToMdError(Exception):
    """Base class for every :mod:`pagetomd` error.

    Subclasses override :attr:`exit_code` and :attr:`hint` to provide stable,
    user-facing failure signals.
    """

    exit_code: int = 1
    hint: str = "Unexpected error. Run with --debug for a full traceback."

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message: str = message

    def __str__(self) -> str:
        return f"{self.__class__.__name__}: {self.message}"


class UsageError(PageToMdError):
    """User invoked the CLI incorrectly (bad flags, missing args, etc.)."""

    exit_code = 64
    hint = "Check the command-line arguments. Run with --help for usage."


class FetchError(PageToMdError):
    """Network, DNS, or HTTP-layer failure while fetching a URL."""

    exit_code = 2
    hint = "Network or HTTP failure. Check the URL, your connection, or retry."


class RobotsDisallowedError(FetchError):
    """The target URL is disallowed by ``robots.txt``."""

    exit_code = 2
    hint = "Blocked by robots.txt. Pass --no-respect-robots to override (use responsibly)."


class ExtractionEmptyError(PageToMdError):
    """The extractor produced no readable content for the page."""

    exit_code = 3
    hint = "Extractor produced no readable content. Try --include-comments or --fetcher playwright."


class ConversionError(PageToMdError):
    """HTML → Markdown conversion failed."""

    exit_code = 3
    hint = "Failed to convert HTML to Markdown. Please report this with the URL."


class WriteError(PageToMdError):
    """Writing the final Markdown output to disk failed."""

    exit_code = 4
    hint = "Failed to write the output file. Check the path and permissions."


class ConfigError(PageToMdError):
    """Configuration is invalid (env vars, CLI flags, or merged result)."""

    exit_code = 64
    hint = "Invalid configuration. Check environment variables and CLI flags."


class DependencyMissingError(PageToMdError):
    """An optional dependency required for the requested feature is absent."""

    exit_code = 5
    hint = (
        "An optional dependency is missing. Install the relevant extra (e.g. pagetomd[playwright])."
    )
