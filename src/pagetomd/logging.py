"""Structured logging setup for :mod:`pagetomd`."""

from __future__ import annotations

import logging
import sys
from typing import Final

import structlog

__all__ = ["configure_logging", "get_logger"]

# Third-party libraries that log verbosely at DEBUG/INFO via the stdlib
# :mod:`logging` module. trafilatura (and its dependencies courlan / htmldate /
# readability) emit hundreds of internal diagnostics per page — e.g.
# ``list link text/total: 15/15``, ``discarding element: slot None``,
# ``Recovering wild text elements``, ``extra in p: …``, ``unknown attribute: …``.
# None of these are actionable for pagetomd users, so we pin each logger to a
# floor level to keep the console readable. See :func:`_quiet_noisy_libraries`.
_NOISY_LIBRARY_LOGGERS: Final[tuple[str, ...]] = (
    "trafilatura",
    "courlan",
    "htmldate",
    "readability",
)

# Floor applied to the noisy library loggers when our own level is INFO or
# higher. Pinning to WARNING silences the DEBUG/INFO firehose without
# additionally suppressing library warnings: at app levels of WARNING or below,
# those warnings still reach the root handler and surface. (At ERROR/CRITICAL
# the root handler — installed by ``basicConfig`` — drops them like everything
# else, since the noisy loggers have no handler of their own and propagate.)
_NOISY_LIBRARY_FLOOR: Final[int] = logging.WARNING


def _quiet_noisy_libraries(app_level: int) -> None:
    """Pin verbose third-party loggers so their DEBUG/INFO noise is silenced.

    The suppression is *overridable by verbosity*: when the application runs at
    ``DEBUG`` (e.g. via ``--debug`` / ``--log-level=debug``) the library loggers
    are allowed to emit at the application's own level, so a developer who
    explicitly asked for debug output also sees trafilatura's internals. When
    the application level is ``INFO`` or higher, each library logger is pinned to
    :data:`_NOISY_LIBRARY_FLOOR` (``WARNING``) so the per-page firehose
    (``list link text``, ``extra in p``, ``Recovering wild text elements``, …)
    disappears.

    The floor only changes whether the *library logger* creates a record; the
    record must still pass the root handler installed by ``basicConfig``. So a
    library WARNING surfaces when the application level is ``WARNING`` or below,
    but is dropped at ``ERROR``/``CRITICAL`` along with everything else — the
    floor never re-raises records above the application's own threshold.

    Args:
        app_level: The numeric stdlib level the application itself is using
            (e.g. :data:`logging.INFO`). When this is ``DEBUG`` the library
            loggers inherit it verbatim (the developer asked for everything);
            for any level at ``INFO`` or higher the libraries are pinned to the
            WARNING floor, which silences the INFO/DEBUG firehose while leaving
            library warnings subject to the application's own level (visible at
            ``warning``/``info``, dropped at ``error``).
    """
    effective = app_level if app_level <= logging.DEBUG else _NOISY_LIBRARY_FLOOR
    for logger_name in _NOISY_LIBRARY_LOGGERS:
        logging.getLogger(logger_name).setLevel(effective)


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

    # Silence verbose third-party libraries (trafilatura et al.) unless the
    # user explicitly asked for debug output. Must run *after* basicConfig so
    # our per-logger floor wins over the root level it just set.
    _quiet_noisy_libraries(numeric_level)

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
