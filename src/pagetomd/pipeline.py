"""End-to-end pipeline orchestrating fetch → extract → convert → write.

Synchronous, CLI-agnostic. Typed errors bubble up unchanged; bare exceptions
are wrapped as :class:`~pagetomd.exceptions.PageToMdError`.
"""

from __future__ import annotations

import secrets
import time
import types
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup

from pagetomd.config import Config
from pagetomd.converter import convert
from pagetomd.exceptions import (
    ExtractionEmptyError,
    PageToMdError,
)
from pagetomd.extractor import extract
from pagetomd.fetcher import FetchedDoc, Fetcher, HttpxFetcher, PlaywrightFetcher
from pagetomd.logging import get_logger
from pagetomd.postprocess import postprocess
from pagetomd.ssrf import redact_url
from pagetomd.writer import (
    Frontmatter,
    build_frontmatter,
    serialize_frontmatter,
    slugify_default_path,
    write_output,
)

__all__ = ["PipelineResult", "run"]

# Threshold (in characters of post-strip body text) below which an httpx
# fetch is considered "SPA-shell shaped" — combined with the marker check
# in :func:`_should_fallback_to_playwright`, this triggers the auto
# fallback to Playwright.
_SPA_BODY_TEXT_THRESHOLD: Final[int] = 200

# Substrings (case-insensitive) that strongly suggest the page is a JS-rendered
# SPA shell. Matching any one — combined with a sparse body — triggers the
# Playwright fallback in ``auto`` mode.
_SPA_MARKERS: Final[tuple[str, ...]] = (
    "data-vue-",
    "data-reactroot",
    "ng-app",
    "ng-version",
    '<div id="app"',
    '<div id="root"',
    '<div id="__next"',
    '<div id="__nuxt"',
    "<noscript>you need to enable javascript",
)

_STDOUT_SENTINEL = Path("-")


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Outcome of a successful end-to-end conversion run."""

    output_path: Path | None
    bytes_written: int
    final_url: str
    title: str | None
    elapsed_ms: int


def run(config: Config, *, fetcher: Fetcher | None = None) -> PipelineResult:
    """Execute the full fetch → extract → convert → write pipeline.

    Args:
        config: Fully validated :class:`~pagetomd.config.Config` driving
            every stage (URL, output sink, network knobs, conversion
            flags, etc.).
        fetcher: Optional pre-built fetcher. When provided, the caller
            owns its lifecycle — the pipeline will **not** open or close
            it. When omitted, the pipeline instantiates the fetcher named
            by ``config.fetcher`` and (for ``httpx``) drives it through
            ``with`` so the underlying ``httpx.Client`` is always closed.

    Returns:
        A :class:`PipelineResult` with output path, bytes written,
        resolved URL, title, and elapsed time.

    Raises:
        Typed :class:`~pagetomd.exceptions.PageToMdError` subclasses.
    """
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        url=redact_url(config.url),
        run_id=secrets.token_hex(4),
    )

    log = get_logger(__name__)

    start_s = time.perf_counter()
    target: Path | None = None
    try:
        target = _resolve_initial_target(config.output)
        log.info(
            "pipeline.start",
            fetcher=config.fetcher,
            output=_describe_target(target),
        )

        if fetcher is not None:
            # Caller owns the lifecycle — just use it.
            return _run_with_fetcher(config, fetcher, start_s, log)

        with _select_fetcher(config) as owned:
            return _run_with_fetcher(config, owned, start_s, log)
    except PageToMdError:
        raise
    except Exception as exc:
        raise PageToMdError(
            "Unexpected pipeline failure",
            original=str(exc),
        ) from exc
    finally:
        structlog.contextvars.clear_contextvars()


def _run_with_fetcher(
    config: Config,
    fetcher: Fetcher,
    start_s: float,
    log: structlog.stdlib.BoundLogger,
) -> PipelineResult:
    """Drive the stage sequence against an already-prepared fetcher."""
    fetched = fetcher.fetch(config.url)
    try:
        extracted = extract(fetched, config)
    except ExtractionEmptyError:
        if not isinstance(fetcher, _AutoFetcher):
            raise
        # The SPA-marker heuristic missed this page — retry with Playwright.
        log.info(
            "fetch.auto.extraction_fallback",
            url=redact_url(config.url),
            reason="extraction_empty_after_httpx",
        )
        fetched = fetcher.fetch_playwright(config.url)
        extracted = extract(fetched, config)
    raw_md = convert(extracted.cleaned_html, config)
    effective_base = _resolve_base_url(base_href=extracted.base_href, final_url=fetched.final_url)
    body_md = postprocess(
        raw_md,
        base_url=effective_base,
        title=extracted.title,
    )
    frontmatter = build_frontmatter(
        fetched,
        extracted,
        include_fetched_at=not config.no_fetched_at,
    )

    if config.output is None:
        target: Path | None = slugify_default_path(fetched, extracted)
    elif _is_stdout(config.output):
        target = _STDOUT_SENTINEL
    else:
        target = config.output

    output_path = write_output(
        body_md,
        frontmatter,
        output=target,
        overwrite=config.overwrite,
        follow_symlinks=config.follow_symlinks,
    )

    bytes_written = _bytes_written(frontmatter, body_md)
    elapsed_ms = int((time.perf_counter() - start_s) * 1000)

    log.info(
        "pipeline.ok",
        elapsed_ms=elapsed_ms,
        bytes_written=bytes_written,
        output_path=str(output_path) if output_path is not None else "stdout",
    )
    return PipelineResult(
        output_path=output_path,
        bytes_written=bytes_written,
        final_url=fetched.final_url,
        title=extracted.title,
        elapsed_ms=elapsed_ms,
    )


def _resolve_initial_target(output: Path | None) -> Path | None:
    """Best-effort target for the ``pipeline.start`` log event."""
    if output is None:
        return None
    if _is_stdout(output):
        return _STDOUT_SENTINEL
    return output


def _describe_target(target: Path | None) -> str:
    """Stringify ``target`` for inclusion in a structured log field."""
    if target is None:
        return "<default>"
    if _is_stdout(target):
        return "stdout"
    return str(target)


def _is_stdout(output: Path | str) -> bool:
    """Return ``True`` when ``output`` is the stdout sentinel ``"-"``."""
    return str(output) == "-"


def _resolve_base_url(*, base_href: str | None, final_url: str) -> str:
    """Return ``base_href`` resolved against ``final_url``, or ``final_url`` itself."""
    if not base_href:
        return final_url
    return urljoin(final_url, base_href)


def _bytes_written(frontmatter: Frontmatter, body_md: str) -> int:
    """Predict the UTF-8 byte length of the rendered output document."""
    head = serialize_frontmatter(frontmatter)
    body = body_md.rstrip("\n") + "\n"
    return len(f"{head}\n{body}".encode())


def _select_fetcher(config: Config) -> AbstractContextManager[Fetcher]:
    """Return a context manager that yields the configured fetcher backend."""
    if config.fetcher == "httpx":
        return HttpxFetcher(config)
    if config.fetcher == "playwright":
        return PlaywrightFetcher(config)
    if config.fetcher == "auto":
        return _AutoFetcher(config)
    # ``Config.fetcher`` is a Literal so this branch is unreachable via the
    # normal validation path. Guard anyway for defence-in-depth.
    raise ValueError(f"Unknown fetcher: {config.fetcher}")  # pragma: no cover


def _should_fallback_to_playwright(html: str) -> bool:
    """Return ``True`` when ``html`` looks like an unrendered SPA shell.

    Requires both a sparse ``<body>`` (below :data:`_SPA_BODY_TEXT_THRESHOLD`)
    and at least one SPA marker to fire, keeping the false-positive rate low.
    """
    if not html:
        return False

    # Extract body text via bs4 with lxml to mirror the extractor's
    # parser choice (avoids subtle differences in entity handling).
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - defensive against bad input
        return False
    body = soup.body
    body_text_len = len(body.get_text(strip=True)) if body is not None else 0
    if body_text_len >= _SPA_BODY_TEXT_THRESHOLD:
        return False

    haystack = html.lower()
    return any(marker in haystack for marker in _SPA_MARKERS)


class _AutoFetcher:
    """Context manager wrapping ``httpx`` with lazy ``playwright`` fallback."""

    def __init__(self, config: Config) -> None:
        """Capture ``config``; defer fetcher construction until ``__enter__``."""
        self._config = config
        self._httpx: HttpxFetcher | None = None
        self._playwright: PlaywrightFetcher | None = None
        self._log = get_logger(__name__)

    def __enter__(self) -> _AutoFetcher:
        """Start the httpx fetcher only — playwright is lazy."""
        self._httpx = HttpxFetcher(self._config).__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        """Close both fetchers if they were started, even on exception."""
        self.close()

    def close(self) -> None:
        """Tear down both backends; safe to call repeatedly."""
        if self._httpx is not None:
            try:
                self._httpx.close()
            finally:
                self._httpx = None
        if self._playwright is not None:
            try:
                self._playwright.close()
            finally:
                self._playwright = None

    def fetch(self, url: str) -> FetchedDoc:
        """Fetch via httpx; transparently retry via playwright on SPA shells."""
        if self._httpx is None:
            self._httpx = HttpxFetcher(self._config)
        doc = self._httpx.fetch(url)
        if not _should_fallback_to_playwright(doc.html):
            return doc
        self._log.info(
            "fetch.auto.fallback",
            url=redact_url(url),
            reason="spa_shell_detected",
            body_chars=_body_char_count(doc.html),
        )
        return self.fetch_playwright(url)

    def fetch_playwright(self, url: str) -> FetchedDoc:
        """Fetch via playwright, lazily initialising the backend."""
        if self._playwright is None:
            self._playwright = PlaywrightFetcher(self._config)
        return self._playwright.fetch(url)


def _body_char_count(html: str) -> int:
    """Return the post-strip length of the ``<body>`` text — for logging only."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover - defensive against bad input
        return 0
    body = soup.body
    return len(body.get_text(strip=True)) if body is not None else 0
