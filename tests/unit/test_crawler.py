"""Unit tests for :mod:`pagetomd.crawler` link extraction and data structures."""

from __future__ import annotations

from pathlib import Path

from pagetomd.crawler import CrawlResult, extract_links, relative_path_from_url

# Seed URL used across the link-extraction tests. Trailing slash makes the
# seed itself the "directory" form so all the canned hrefs that look like
# ``/docs/page-a`` land directly inside the seed's subtree.
_SEED = "https://example.com/docs/"


def test_extract_links_same_prefix() -> None:
    """Only links under the seed's subtree are returned."""
    html = """
    <html><body>
      <a href="/docs/page-a">A</a>
      <a href="/docs/page-b">B</a>
      <a href="/other/page">Other</a>
      <a href="https://external.com/page">External</a>
    </body></html>
    """
    links = extract_links(html, base_url=_SEED, seed_url=_SEED)
    assert "https://example.com/docs/page-a" in links
    assert "https://example.com/docs/page-b" in links
    assert "https://example.com/other/page" not in links
    assert "https://external.com/page" not in links


def test_extract_links_strips_fragment() -> None:
    html = '<a href="/docs/page#section">X</a>'
    links = extract_links(html, base_url=_SEED, seed_url=_SEED)
    assert "https://example.com/docs/page" in links
    assert all("#" not in link for link in links)


def test_extract_links_deduplicates() -> None:
    html = '<a href="/docs/page">A</a><a href="/docs/page">B</a>'
    links = extract_links(html, base_url=_SEED, seed_url=_SEED)
    assert links.count("https://example.com/docs/page") == 1


def test_extract_links_excludes_seed() -> None:
    """Links pointing back at the seed itself are dropped to avoid re-fetching."""
    html = '<a href="/docs/">Self</a><a href="/docs/other">Other</a>'
    links = extract_links(html, base_url=_SEED, seed_url=_SEED)
    assert "https://example.com/docs/" not in links
    assert "https://example.com/docs" not in links


def test_extract_links_rejects_seed_siblings() -> None:
    """A leaf seed scopes the crawl to its own subtree (siblings excluded)."""
    seed = "https://example.com/docs/seed"
    html = """
    <html><body>
      <a href="/docs/seed/child">Child</a>
      <a href="/docs/other">Sibling (out of scope)</a>
    </body></html>
    """
    links = extract_links(html, base_url=seed, seed_url=seed)
    assert "https://example.com/docs/seed/child" in links
    assert "https://example.com/docs/other" not in links


def test_crawl_result_fields() -> None:
    r = CrawlResult(pages_written=3, pages_skipped=1, pages_failed=2, output_dir=None)
    assert r.pages_written == 3
    assert r.total == 6


# ---------------------------------------------------------------------------
# relative_path_from_url — URL-mirrored output structure
# ---------------------------------------------------------------------------


class TestRelativePathFromUrl:
    """The seed-as-root model maps each URL to a deterministic relative path."""

    def test_seed_itself_maps_to_index(self) -> None:
        assert relative_path_from_url(
            "https://example.com/docs/seed",
            seed_url="https://example.com/docs/seed",
        ) == Path("index.md")

    def test_seed_with_trailing_slash_maps_to_index(self) -> None:
        assert relative_path_from_url(
            "https://example.com/docs/seed/",
            seed_url="https://example.com/docs/seed",
        ) == Path("index.md")

    def test_direct_child_is_flat_md(self) -> None:
        assert relative_path_from_url(
            "https://example.com/docs/seed/intro",
            seed_url="https://example.com/docs/seed",
        ) == Path("intro.md")

    def test_child_with_trailing_slash_is_index_inside_subdir(self) -> None:
        assert relative_path_from_url(
            "https://example.com/docs/seed/intro/",
            seed_url="https://example.com/docs/seed",
        ) == Path("intro/index.md")

    def test_nested_url_mirrors_directory_structure(self) -> None:
        assert relative_path_from_url(
            "https://example.com/docs/seed/concepts/alerts",
            seed_url="https://example.com/docs/seed",
        ) == Path("concepts/alerts.md")

    def test_deep_nested_with_trailing_slash(self) -> None:
        assert relative_path_from_url(
            "https://example.com/docs/seed/concepts/alerts/",
            seed_url="https://example.com/docs/seed",
        ) == Path("concepts/alerts/index.md")

    def test_directory_style_seed(self) -> None:
        """A seed already ending in / treats its own root the same way."""
        assert relative_path_from_url(
            "https://example.com/docs/",
            seed_url="https://example.com/docs/",
        ) == Path("index.md")
        assert relative_path_from_url(
            "https://example.com/docs/intro",
            seed_url="https://example.com/docs/",
        ) == Path("intro.md")

    def test_segments_individually_slugified(self) -> None:
        """Each path segment is slugified on its own."""
        assert relative_path_from_url(
            "https://example.com/docs/seed/My Section/Sub Page",
            seed_url="https://example.com/docs/seed",
        ) == Path("my-section/sub-page.md")

    def test_windows_reserved_segment_guarded(self) -> None:
        """A ``CON`` segment is escaped even when it appears mid-path."""
        result = relative_path_from_url(
            "https://example.com/docs/seed/CON/sub",
            seed_url="https://example.com/docs/seed",
        )
        # The reserved directory name gets the suffix; the leaf is untouched.
        assert result == Path("con-page/sub.md")

    def test_long_segment_truncated(self) -> None:
        long = "a" * 200
        result = relative_path_from_url(
            f"https://example.com/docs/seed/{long}",
            seed_url="https://example.com/docs/seed",
        )
        # Filename stem must fit within the 80-char slug cap + ".md".
        assert len(result.name) <= 84

    def test_collision_prevention_across_parents(self) -> None:
        """Two pages whose last segment is identical no longer collide."""
        a = relative_path_from_url(
            "https://example.com/docs/seed/guide/intro",
            seed_url="https://example.com/docs/seed",
        )
        b = relative_path_from_url(
            "https://example.com/docs/seed/api/intro",
            seed_url="https://example.com/docs/seed",
        )
        assert a == Path("guide/intro.md")
        assert b == Path("api/intro.md")
        assert a != b
