"""Runtime configuration for :mod:`pagetomd`.

The :class:`Config` model is the single source of truth for all knobs the CLI,
pipeline, and adapters consult. It is built on
:class:`pydantic_settings.BaseSettings` so values flow from hard-coded defaults,
``PAGETOMD_*`` env vars, and CLI overrides (in ascending precedence).
"""

from __future__ import annotations

import pathlib
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from pagetomd.exceptions import ConfigError

__all__ = ["Config"]


def _default_user_agent() -> str:
    """Build the default ``User-Agent`` lazily to avoid circular imports."""
    from pagetomd import __version__

    return f"pagetomd/{__version__}"


class Config(BaseSettings):
    """Frozen, validated configuration for a single ``pagetomd`` run."""

    model_config = SettingsConfigDict(
        env_prefix="PAGETOMD_",
        frozen=True,
        extra="forbid",
        validate_default=True,
    )

    url: str
    output: pathlib.Path | None = None
    overwrite: bool = False
    follow_symlinks: bool = False
    fetcher: Literal["httpx", "playwright", "auto"] = "httpx"
    timeout: float = Field(default=30.0, gt=0)
    retries: int = Field(default=3, ge=0)
    user_agent: str = _default_user_agent()
    verify_ssl: bool = True
    respect_robots: bool = True
    follow_redirects: bool = True
    max_redirects: int = Field(default=10, gt=0)
    max_body_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    playwright_idle_ms: int = Field(default=500, ge=0)
    include_comments: bool = False
    include_images: bool = True
    include_links: bool = True
    heading_style: Literal["atx", "setext"] = "atx"
    code_fences: bool = True
    wide_tables: Literal["kv", "html", "drop"] = "kv"
    no_fetched_at: bool = False
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_json: bool = False

    @field_validator("user_agent")
    @classmethod
    def _user_agent_clean(cls, value: str) -> str:
        """Reject CR / LF / NUL bytes and empty values.

        Embedded CR/LF would allow header injection; NUL bytes would be
        silently mangled by the transport.
        """
        if not value or not value.strip():
            raise ValueError("user_agent must be a non-empty string")
        if any(ch in value for ch in ("\r", "\n", "\0")):
            raise ValueError(
                "user_agent must not contain CR, LF, or NUL characters "
                "(would corrupt outbound HTTP headers)"
            )
        return value

    @classmethod
    def from_overrides(cls, cli_overrides: dict[str, object]) -> Config:
        """Construct a :class:`Config` from env vars plus CLI overrides.

        Raises:
            ConfigError: If pydantic rejects any field.
        """
        try:
            # BaseSettings honours env vars automatically; explicit kwargs
            # supplied here take precedence over the env-sourced values.
            kwargs: dict[str, Any] = dict(cli_overrides)
            return cls(**kwargs)
        except ValidationError as exc:
            raise ConfigError(
                "Invalid configuration",
                errors=exc.errors(),
            ) from exc
