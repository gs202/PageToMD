"""Tests for the :func:`pagetomd.crawler.crawl` orchestrator.

Uses ``unittest.mock`` to replace ``pipeline.run`` and the fetcher context
manager so the BFS loop's bookkeeping (written / skipped / failed counts)
can be exercised without any real network or filesystem dependency on the
inner pipeline stages.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pagetomd.config import Config
from pagetomd.crawler import CrawlResult, crawl
from pagetomd.exceptions import FetchError, WriteError


def _make_config(
    url: str = "https://example.com/docs/seed",
    output: Path | None = None,
) -> Config:
    return Config(url=url, output=output, respect_robots=False)


def _ok_result(
    url: str = "https://example.com/docs/seed",
    path: Path = Path("out.md"),
    html: str = "",
) -> MagicMock:
    """Build a :class:`MagicMock` shaped like :class:`PipelineResult`.

    Using a mock keeps Task 3 decoupled from the ``PipelineResult.fetched_html``
    field that Task 5 introduces — the orchestrator only reads attributes,
    not the concrete dataclass type.
    """
    return MagicMock(
        output_path=path,
        final_url=url,
        title="T",
        fetched_html=html,
    )


def test_crawl_single_page_no_links(tmp_path: Path) -> None:
    """Seed page with no discoverable links → writes 1 file."""
    seed_html = "<html><body><p>No links here</p></body></html>"

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = MagicMock(
            html=seed_html,
            final_url="https://example.com/docs/seed",
        )
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)

        mock_pipeline.run.return_value = _ok_result(path=tmp_path / "seed.md", html=seed_html)

        cfg = _make_config(output=tmp_path)
        result = crawl(cfg, max_depth=1)

    assert isinstance(result, CrawlResult)
    assert result.pages_written == 1
    assert result.pages_failed == 0


def test_crawl_skips_failed_pages(tmp_path: Path) -> None:
    """A :class:`FetchError` on a discovered page is counted as failed.

    The retry pass runs automatically and re-attempts the broken page, so
    ``pipeline.run`` is called three times: seed (ok), broken (fail),
    broken-retry (still fails).
    """
    # Link must live under the seed's subtree (``/docs/seed/...``) to be
    # picked up by the new seed-as-root scoping in ``_seed_prefix``.
    seed_html = '<html><body><a href="/docs/seed/broken">Broken</a></body></html>'

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = MagicMock(
            html=seed_html,
            final_url="https://example.com/docs/seed",
        )
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)

        mock_pipeline.run.side_effect = [
            _ok_result(path=tmp_path / "index.md", html=seed_html),
            FetchError("network error"),
            # Retry pass: the broken page still fails.
            FetchError("network error"),
        ]

        cfg = _make_config(output=tmp_path)
        result = crawl(cfg, max_depth=1)

    assert result.pages_written == 1
    assert result.pages_failed == 1


def test_crawl_skips_existing_file_without_overwrite(tmp_path: Path) -> None:
    """``WriteError`` on existing file counts as skipped, not failed."""
    seed_html = '<html><body><a href="/docs/seed/existing">Existing</a></body></html>'

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = MagicMock(
            html=seed_html,
            final_url="https://example.com/docs/seed",
        )
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)

        mock_pipeline.run.side_effect = [
            _ok_result(path=tmp_path / "index.md", html=seed_html),
            WriteError("file exists"),
        ]

        cfg = _make_config(output=tmp_path)
        result = crawl(cfg, max_depth=1)

    assert result.pages_written == 1
    assert result.pages_skipped == 1
    assert result.pages_failed == 0


def test_crawl_respects_max_depth(tmp_path: Path) -> None:
    """With ``max_depth=0``, only the seed page is fetched (no link discovery)."""
    seed_html = '<html><body><a href="/docs/child">Child</a></body></html>'

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher.fetch.return_value = MagicMock(
            html=seed_html,
            final_url="https://example.com/docs/seed",
        )
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)

        mock_pipeline.run.return_value = _ok_result(path=tmp_path / "seed.md", html=seed_html)

        cfg = _make_config(output=tmp_path)
        result = crawl(cfg, max_depth=0)

    assert result.pages_written == 1
    mock_pipeline.run.assert_called_once()


# ---------------------------------------------------------------------------
# Retry-pass tests
# ---------------------------------------------------------------------------


def test_retry_recovers_transient_failure(tmp_path: Path) -> None:
    """Page fails on initial pass then succeeds on retry → no longer in failed_urls."""
    seed_html = '<html><body><a href="/docs/seed/flaky">Flaky</a></body></html>'
    flaky_html = "<html><body><p>Flaky page content</p></body></html>"

    call_count = 0

    def _side_effect(cfg: object, fetcher: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Seed page succeeds.
            return _ok_result(path=tmp_path / "index.md", html=seed_html)
        if call_count == 2:
            # Initial attempt at /flaky fails.
            raise FetchError("transient network error")
        # Retry attempt at /flaky succeeds.
        return _ok_result(
            url="https://example.com/docs/seed/flaky",
            path=tmp_path / "flaky.md",
            html=flaky_html,
        )

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_pipeline.run.side_effect = _side_effect

        cfg = _make_config(output=tmp_path)
        result = crawl(cfg, max_depth=1)

    assert result.pages_written == 2
    assert result.pages_failed == 0
    assert result.failed_urls == []
    assert call_count == 3  # seed + fail + retry-success


def test_retry_persistent_failure_stays_failed(tmp_path: Path) -> None:
    """Page that fails on both passes stays in failed_urls; exactly two attempts."""
    seed_html = '<html><body><a href="/docs/seed/broken">Broken</a></body></html>'

    call_count = 0

    def _side_effect(cfg: object, fetcher: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ok_result(path=tmp_path / "index.md", html=seed_html)
        # Both initial and retry attempts fail.
        raise FetchError("permanent error")

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_pipeline.run.side_effect = _side_effect

        cfg = _make_config(output=tmp_path)
        result = crawl(cfg, max_depth=1)

    assert result.pages_failed == 1
    assert "https://example.com/docs/seed/broken" in result.failed_urls
    assert call_count == 3  # seed + initial-fail + retry-fail


def test_retry_discovers_new_links(tmp_path: Path) -> None:
    """Retry of a failed page succeeds and its child links are followed."""
    seed_html = '<html><body><a href="/docs/seed/parent">Parent</a></body></html>'
    parent_html = '<html><body><a href="/docs/seed/parent/child">Child</a></body></html>'
    child_html = "<html><body><p>Child content</p></body></html>"

    call_count = 0

    def _side_effect(cfg: object, fetcher: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Seed succeeds.
            return _ok_result(path=tmp_path / "index.md", html=seed_html)
        if call_count == 2:
            # Initial attempt at /parent fails.
            raise FetchError("transient error")
        if call_count == 3:
            # Retry: /parent succeeds and exposes a child link.
            return _ok_result(
                url="https://example.com/docs/seed/parent",
                path=tmp_path / "parent.md",
                html=parent_html,
            )
        # Child page discovered during retry succeeds.
        return _ok_result(
            url="https://example.com/docs/seed/parent/child",
            path=tmp_path / "parent" / "child.md",
            html=child_html,
        )

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_pipeline.run.side_effect = _side_effect

        cfg = _make_config(output=tmp_path)
        # max_depth=2 so the child under /parent can be reached.
        result = crawl(cfg, max_depth=2)

    assert result.pages_written == 3  # seed + parent (retry) + child
    assert result.pages_failed == 0
    assert call_count == 4


def test_retry_disabled_flag(tmp_path: Path) -> None:
    """``retry_failed=False`` disables the retry pass entirely."""
    seed_html = '<html><body><a href="/docs/seed/broken">Broken</a></body></html>'

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)

        mock_pipeline.run.side_effect = [
            _ok_result(path=tmp_path / "index.md", html=seed_html),
            FetchError("network error"),
        ]

        cfg = _make_config(output=tmp_path)
        result = crawl(cfg, max_depth=1, retry_failed=False)

    assert result.pages_written == 1
    assert result.pages_failed == 1
    assert "https://example.com/docs/seed/broken" in result.failed_urls
    # Only two calls: seed + broken; no retry call.
    assert mock_pipeline.run.call_count == 2


def test_retry_preserves_original_depth(tmp_path: Path) -> None:
    """Retry re-enqueues failed URL at its original depth so the depth budget is correct.

    Setup: seed at depth 0 links to /parent at depth 1.  /parent fails on
    the initial pass.  max_depth=2.  On retry, /parent succeeds and yields
    a child link.  Because /parent is at depth 1 and max_depth=2, the child
    at depth 2 should be enqueued and fetched.
    """
    seed_html = '<html><body><a href="/docs/seed/parent">Parent</a></body></html>'
    parent_html = '<html><body><a href="/docs/seed/parent/deep">Deep</a></body></html>'
    deep_html = "<html><body><p>Deep content</p></body></html>"

    call_count = 0

    def _side_effect(cfg: object, fetcher: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ok_result(path=tmp_path / "index.md", html=seed_html)
        if call_count == 2:
            raise FetchError("transient")
        if call_count == 3:
            # Retry: /parent succeeds.
            return _ok_result(
                url="https://example.com/docs/seed/parent",
                path=tmp_path / "parent.md",
                html=parent_html,
            )
        # Deep child at depth 2.
        return _ok_result(
            url="https://example.com/docs/seed/parent/deep",
            path=tmp_path / "parent" / "deep.md",
            html=deep_html,
        )

    with (
        patch("pagetomd.crawler.pipeline") as mock_pipeline,
        patch("pagetomd.crawler._open_fetcher") as mock_fetcher_ctx,
    ):
        mock_fetcher = MagicMock()
        mock_fetcher_ctx.return_value.__enter__ = MagicMock(return_value=mock_fetcher)
        mock_fetcher_ctx.return_value.__exit__ = MagicMock(return_value=False)
        mock_pipeline.run.side_effect = _side_effect

        cfg = _make_config(output=tmp_path)
        result = crawl(cfg, max_depth=2)

    # seed + parent (retry-ok) + deep (discovered during retry)
    assert result.pages_written == 3
    assert result.pages_failed == 0
    assert call_count == 4
