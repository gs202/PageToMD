"""End-to-end crawl tests using :mod:`respx` to mock HTTP responses."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from pagetomd.config import Config
from pagetomd.crawler import crawl

# Seed and discovered URLs are crafted so every discovered link lives
# strictly *under* the seed's subtree — matching the seed-as-root scoping
# model implemented in :func:`pagetomd.crawler._seed_prefix`.
SEED_HTML = """
<html><head><title>Seed</title></head><body>
  <article>
    <h1>Seed Page</h1>
    <p>This is the seed page with enough body text for the extractor to keep it.
    Padding for trafilatura's recall heuristics so we never trip ExtractionEmptyError.</p>
    <ul>
      <li><a href="/docs/seed/page-a">Page A</a></li>
      <li><a href="/docs/seed/guide/intro">Nested Intro</a></li>
      <li><a href="/docs/other">Sibling (out of scope)</a></li>
      <li><a href="https://external.example.com/x">External (out of scope)</a></li>
    </ul>
  </article>
</body></html>
"""

PAGE_A_HTML = """
<html><head><title>Page A</title></head><body>
  <article>
    <h1>Page A</h1>
    <p>Page A body content with sufficient material to satisfy the extractor's
    recall heuristics, even across multiple sentences and paragraphs. Padding
    padding padding padding padding padding padding padding padding padding.</p>
  </article>
</body></html>
"""

NESTED_INTRO_HTML = """
<html><head><title>Nested Intro</title></head><body>
  <article>
    <h1>Nested Intro</h1>
    <p>Nested page body content with sufficient material to satisfy the
    extractor's recall heuristics, even across multiple sentences and
    paragraphs. Padding padding padding padding padding padding padding.</p>
  </article>
</body></html>
"""

_HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8"}


@pytest.mark.integration
@respx.mock
def test_crawl_writes_mirrored_directory_tree(tmp_path: Path) -> None:
    """Crawl seed + 2 linked pages → ``.md`` files mirroring the URL hierarchy."""
    respx.get("https://example.com/docs/seed").mock(
        return_value=httpx.Response(200, html=SEED_HTML, headers=_HTML_HEADERS)
    )
    respx.get("https://example.com/docs/seed/page-a").mock(
        return_value=httpx.Response(200, html=PAGE_A_HTML, headers=_HTML_HEADERS)
    )
    respx.get("https://example.com/docs/seed/guide/intro").mock(
        return_value=httpx.Response(200, html=NESTED_INTRO_HTML, headers=_HTML_HEADERS)
    )

    cfg = Config(
        url="https://example.com/docs/seed",
        output=tmp_path,
        respect_robots=False,
        no_fetched_at=True,
    )
    result = crawl(cfg, max_depth=1)

    assert result.pages_written == 3
    assert result.pages_failed == 0

    # Seed → index.md at the root of the output directory.
    assert (tmp_path / "index.md").exists()
    # Direct child → flat file at the root.
    assert (tmp_path / "page-a.md").exists()
    # Nested child → mirrored directory structure.
    assert (tmp_path / "guide" / "intro.md").exists()

    # ``rglob`` is intentional: the output is no longer flat.
    md_files = list(tmp_path.rglob("*.md"))
    assert len(md_files) == 3


@pytest.mark.integration
@respx.mock
def test_crawl_skips_out_of_scope_links(tmp_path: Path) -> None:
    """Sibling and external links must not be fetched."""
    respx.get("https://example.com/docs/seed").mock(
        return_value=httpx.Response(200, html=SEED_HTML, headers=_HTML_HEADERS)
    )
    respx.get("https://example.com/docs/seed/page-a").mock(
        return_value=httpx.Response(200, html=PAGE_A_HTML, headers=_HTML_HEADERS)
    )
    respx.get("https://example.com/docs/seed/guide/intro").mock(
        return_value=httpx.Response(200, html=NESTED_INTRO_HTML, headers=_HTML_HEADERS)
    )
    # ``/docs/other`` and the external host must NOT be fetched —
    # respx raises ``NoMockFound`` if they slip through.

    cfg = Config(
        url="https://example.com/docs/seed",
        output=tmp_path,
        respect_robots=False,
        no_fetched_at=True,
    )
    crawl(cfg, max_depth=1)
    # Reaching this line means the prefix filter held.


FLAKY_PAGE_HTML = """
<html><head><title>Flaky Page</title></head><body>
  <article>
    <h1>Flaky Page</h1>
    <p>This page fails on the first attempt but succeeds on the second.
    Padding for trafilatura's recall heuristics so we never trip ExtractionEmptyError.
    Extra padding extra padding extra padding extra padding.</p>
  </article>
</body></html>
"""

# HTML that links to /docs/seed/flaky (which will fail then succeed).
SEED_WITH_FLAKY_HTML = """
<html><head><title>Seed</title></head><body>
  <article>
    <h1>Seed Page</h1>
    <p>This is the seed page with enough body text for the extractor to keep it.
    Padding for trafilatura's recall heuristics so we never trip ExtractionEmptyError.</p>
    <ul>
      <li><a href="/docs/seed/flaky">Flaky Page</a></li>
    </ul>
  </article>
</body></html>
"""


@pytest.mark.integration
@respx.mock
def test_retry_recovers_failed_page_e2e(tmp_path: Path) -> None:
    """End-to-end: page fails on initial pass, succeeds on retry via counter."""
    # The seed page always returns 200.
    respx.get("https://example.com/docs/seed").mock(
        return_value=httpx.Response(200, html=SEED_WITH_FLAKY_HTML, headers=_HTML_HEADERS)
    )

    # The flaky page returns 500 on the first call and 200 on the second.
    flaky_call_count = 0

    def _flaky_handler(request: httpx.Request) -> httpx.Response:
        nonlocal flaky_call_count
        flaky_call_count += 1
        if flaky_call_count == 1:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, html=FLAKY_PAGE_HTML, headers=_HTML_HEADERS)

    respx.get("https://example.com/docs/seed/flaky").mock(side_effect=_flaky_handler)

    cfg = Config(
        url="https://example.com/docs/seed",
        output=tmp_path,
        respect_robots=False,
        no_fetched_at=True,
        retries=0,  # No per-page retries; only the crawl-level retry pass.
    )
    result = crawl(cfg, max_depth=1)

    assert result.pages_written == 2  # seed + flaky (recovered on retry)
    assert result.pages_failed == 0
    assert result.failed_urls == []
    assert (tmp_path / "index.md").exists()
    assert (tmp_path / "flaky.md").exists()
