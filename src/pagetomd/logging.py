"""Structured logging setup for :mod:`pagetomd`."""

from __future__ import annotations

import logging
import sys

import structlog

__all__ = ["configure_logging", "get_logger"]


def configure_logging(level: str = "info", json_mode: bool = False) -> None:
    """Configure :mod:`structlog` and the stdlib logging root.

    Args:
        level: Minimum log level, case-insensitive. Accepts the usual stdlib
            level names (``debug``, ``info``, ``warning``, ``error``,
            ``critical``).
        json_mode: When true, render each record as a single JSON line via
            :class:`structlog.processors.JSONRenderer`. Otherwise use the
            human-friendly :class:`structlog.dev.ConsoleRenderer` (with
            colours when stderr is a TTY).
    """
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        # Fall back to INFO when an unknown level name slips through; we
        # never want logging setup to itself raise.
        numeric_level = logging.INFO

    # Mirror the level onto the stdlib root so any third-party library that
    # logs via :mod:`logging` honours the same threshold.
    logging.basicConfig(
        level=numeric_level,
        stream=sys.stderr,
        format="%(message)s",
        force=True,
    )

    renderer: structlog.types.Processor
    if json_mode:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a configured structlog logger."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
