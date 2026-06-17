"""Site-crawl support for :mod:`pagetomd`.

Provides link extraction from rendered HTML and the top-level
:func:`crawl` orchestrator that converts an entire documentation
site into a directory of Markdown files.
"""

from __future__ import annotations

import collections
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from slugify import slugify

from pagetomd import pipeline
from pagetomd.exceptions import PageToMdError, WriteError
from pagetomd.logging import get_logger
from pagetomd.pipeline import _select_fetcher

if TYPE_CHECKING:  # pragma: no cover - import only used for type hints
    from pagetomd.config import Config
    from pagetomd.fetcher import Fetcher

__all__ = ["CrawlResult", "crawl", "extract_links", "relative_path_from_url"]

_log = get_logger(__name__)

_SLUG_MAX_LENGTH = 80
# Windows reserved device-name stems (case-insensitive); files literally
# named after these collide with DOS devices on Windows even when given
# an extension. We mirror the guard in :mod:`pagetomd.writer`.
_WINDOWS_RESERVED_STEMS: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(10)),
        *(f"lpt{i}" for i in range(10)),
    }
)


@dataclass
class CrawlResult:
    """Summary of a completed crawl run."""

    pages_written: int
    pages_skipped: int
    pages_failed: int
    output_dir: Path | None
    output_paths: list[Path] = field(default_factory=list)
    skipped_urls: list[str] = field(default_factory=list)
    failed_urls: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total pages attempted."""
        return self.pages_written + self.pages_skipped + self.pages_failed


def _normalize_url(url: str) -> str:
    """Strip the fragment and normalise the trailing slash for deduplication.

    Args:
        url: Absolute URL to normalise.

    Returns:
        A canonical form with the fragment removed and (for non-root paths)
        any single trailing slash trimmed so ``/foo`` and ``/foo/`` dedupe.
    """
    parts = urlsplit(url)
    normalized = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    return normalized.rstrip("/") if parts.path != "/" else normalized


def _seed_prefix(seed_url: str) -> str:
    """Return the URL prefix defining the seed's subtree.

    The seed is treated as the *root* of its own subtree, so only its
    descendants are accepted as crawl targets. A seed of
    ``https://example.com/docs/seed`` yields the prefix
    ``https://example.com/docs/seed/`` (note the appended slash). Siblings
    such as ``/docs/other`` are out of scope.

    A seed already ending in ``/`` is left as-is so a "directory" URL is
    its own root.

    Args:
        seed_url: The seed URL supplied to :func:`crawl`.

    Returns:
        An absolute URL ending in ``/`` suitable for ``startswith`` matching.
    """
    parts = urlsplit(seed_url)
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def extract_links(html: str, *, base_url: str, seed_url: str) -> list[str]:
    """Extract same-prefix absolute URLs from *html*.

    Args:
        html: Rendered HTML string (may include shadow-DOM-serialised content).
        base_url: The URL of the page being parsed (used to resolve relative hrefs).
        seed_url: The original seed URL; only links sharing its path prefix
            are kept.

    Returns:
        Deduplicated list of absolute URLs (fragments stripped, seed excluded),
        in document order of first occurrence.
    """
    prefix = _seed_prefix(seed_url)
    seed_norm = _normalize_url(seed_url)

    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    result: list[str] = []

    for tag in soup.find_all("a", href=True):
        # bs4 returns ``str | AttributeValueList`` for indexed attribute
        # access; coerce to ``str`` so the rest of the loop stays simple.
        href = str(tag["href"]).strip()
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue
        absolute = urljoin(base_url, href)
        normalized = _normalize_url(absolute)
        if normalized == seed_norm:
            continue
        if not normalized.startswith(prefix):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)

    return result


def _slugify_segment(raw: str) -> str:
    """Slugify a single URL path segment, applying Windows-reserved guard.

    Returns the empty string when the input cannot be reduced to anything
    useful; callers decide on the fallback (e.g. ``"page"``).
    """
    slug = slugify(raw, max_length=_SLUG_MAX_LENGTH, lowercase=True, word_boundary=True)
    if slug.lower() in _WINDOWS_RESERVED_STEMS:
        slug = f"{slug}-page"
    return slug


def relative_path_from_url(url: str, *, seed_url: str) -> Path:
    """Derive a ``*.md`` output path mirroring the URL hierarchy under ``seed_url``.

    The returned path is **relative** to the crawl's output directory and
    is intended to be joined with it by the caller.

    Mapping rules (for a seed of ``https://example.com/docs/seed``):

    +------------------------------------------------+--------------------------------+
    | URL                                            | Relative output path           |
    +================================================+================================+
    | ``…/docs/seed`` (= the seed itself)            | ``index.md``                   |
    +------------------------------------------------+--------------------------------+
    | ``…/docs/seed/``                               | ``index.md``                   |
    +------------------------------------------------+--------------------------------+
    | ``…/docs/seed/intro``                          | ``intro.md``                   |
    +------------------------------------------------+--------------------------------+
    | ``…/docs/seed/intro/``                         | ``intro/index.md``             |
    +------------------------------------------------+--------------------------------+
    | ``…/docs/seed/concepts/alerts``                | ``concepts/alerts.md``         |
    +------------------------------------------------+--------------------------------+

    Each path segment is slugified independently so reserved characters and
    Windows-reserved device names (``CON``, ``PRN``, …) are escaped on a
    per-segment basis without losing the directory hierarchy.

    Args:
        url: Absolute URL of the page being saved.
        seed_url: The crawl's seed URL (used to determine the relative root).

    Returns:
        A relative :class:`Path` ending in ``.md``. Always contains at
        least one path component.
    """
    seed_parts = urlsplit(seed_url)
    seed_path = seed_parts.path if seed_parts.path.endswith("/") else seed_parts.path + "/"

    url_parts = urlsplit(url)
    url_path = url_parts.path

    # Compute the part of ``url_path`` that lives under the seed's subtree.
    # If the URL does not start with the seed root, fall back to the full
    # URL path so the caller still gets a deterministic file location.
    if url_path == seed_path.rstrip("/") or url_path == seed_path:
        # The URL *is* the seed (with or without trailing slash).
        return Path("index.md")
    if url_path.startswith(seed_path):
        relative_raw = url_path[len(seed_path) :]
    else:
        # Should not normally happen — ``extract_links`` filters by prefix —
        # but guard so an in-scope check failure does not produce a path
        # that escapes the output directory.
        relative_raw = url_path.lstrip("/")

    # A trailing slash means "directory page" → append ``index`` so the
    # last segment becomes the filename stem.
    trailing_slash = relative_raw.endswith("/")
    raw_segments = [s for s in relative_raw.split("/") if s]
    if trailing_slash or not raw_segments:
        raw_segments.append("index")

    slug_segments: list[str] = [_slugify_segment(s) or "page" for s in raw_segments]
    # All but the last segment become directories; the last becomes the
    # ``*.md`` filename stem.
    *dirs, stem = slug_segments
    return Path(*dirs, f"{stem}.md") if dirs else Path(f"{stem}.md")


@contextmanager
def _open_fetcher(config: Config) -> Iterator[Fetcher]:
    """Thin wrapper around :func:`pagetomd.pipeline._select_fetcher`.

    Exists as a stable seam so unit tests can monkeypatch fetcher
    construction without reaching into the pipeline module.
    """
    with _select_fetcher(config) as fetcher:
        yield fetcher


def crawl(config: Config, *, max_depth: int = 1) -> CrawlResult:
    """Crawl all pages reachable from ``config.url`` up to *max_depth* hops.

    A single fetcher context is opened for the entire crawl so browser
    backends (Playwright) do not relaunch Chromium per page.

    Args:
        config: Base configuration. ``config.url`` is the seed URL.
            ``config.output`` must be a directory path (or ``None`` for CWD).
        max_depth: Maximum BFS depth from the seed. ``0`` fetches only the
            seed page. ``1`` (default) also fetches pages linked from the
            seed.

    Returns:
        A :class:`CrawlResult` summarising pages written, skipped, and failed.
    """
    output_dir: Path = config.output if config.output is not None else Path()
    output_dir.mkdir(parents=True, exist_ok=True)

    seed_url = _normalize_url(config.url)
    # BFS queue carries ``(url, depth)`` tuples; ``visited`` guards against
    # revisits across the whole crawl, not just the current depth band.
    queue: collections.deque[tuple[str, int]] = collections.deque([(seed_url, 0)])
    visited: set[str] = {seed_url}

    pages_written = 0
    pages_skipped = 0
    pages_failed = 0
    output_paths: list[Path] = []
    skipped_urls = []
    failed_urls = []

    crawl_id = secrets.token_hex(4)
    _log.info("crawl.start", seed=seed_url, max_depth=max_depth, crawl_id=crawl_id)

    with _open_fetcher(config) as fetcher:
        while queue:
            url, depth = queue.popleft()
            # Mirror the URL hierarchy as a directory tree under the output
            # dir; the writer's ``_ensure_parent_dir`` (called from
            # ``write_output``) creates every intermediate directory.
            relative = relative_path_from_url(url, seed_url=seed_url)
            dest = output_dir / relative

            _log.info(
                "crawl.page.start",
                url=url,
                depth=depth,
                dest=str(dest),
                crawl_id=crawl_id,
            )

            # Config is frozen — derive a per-page copy via ``model_copy``.
            page_config = config.model_copy(update={"url": url, "output": dest})

            try:
                result = pipeline.run(page_config, fetcher=fetcher)
            except WriteError as exc:
                # Existing file without --overwrite is not a hard failure —
                # log it and continue with the rest of the crawl.
                pages_skipped += 1
                skipped_urls.append(url)
                _log.warning(
                    "crawl.page.skip",
                    url=url,
                    reason=str(exc),
                    crawl_id=crawl_id,
                )
                continue
            except PageToMdError as exc:
                pages_failed += 1
                failed_urls.append(url)
                _log.error(
                    "crawl.page.error",
                    url=url,
                    error=str(exc),
                    crawl_id=crawl_id,
                )
                continue

            pages_written += 1
            if result.output_path:
                output_paths.append(result.output_path)
            _log.info("crawl.page.ok", url=url, depth=depth, crawl_id=crawl_id)

            if depth < max_depth:
                # ``PipelineResult.fetched_html`` carries the HTML from the
                # fetch stage we just paid for, so link extraction does NOT
                # need to refetch the page. Fall back to an empty string if
                # for any reason the pipeline omitted it.
                fetched_html = result.fetched_html or ""
                for link in extract_links(
                    fetched_html,
                    base_url=result.final_url,
                    seed_url=seed_url,
                ):
                    norm = _normalize_url(link)
                    if norm not in visited:
                        visited.add(norm)
                        queue.append((norm, depth + 1))
                        _log.debug(
                            "crawl.link.queued",
                            url=norm,
                            depth=depth + 1,
                            crawl_id=crawl_id,
                        )

    _log.info(
        "crawl.done",
        pages_written=pages_written,
        pages_skipped=pages_skipped,
        pages_failed=pages_failed,
        crawl_id=crawl_id,
    )
    return CrawlResult(
        pages_written=pages_written,
        pages_skipped=pages_skipped,
        pages_failed=pages_failed,
        output_dir=output_dir,
        output_paths=output_paths,
        skipped_urls=skipped_urls,
        failed_urls=failed_urls,
    )
