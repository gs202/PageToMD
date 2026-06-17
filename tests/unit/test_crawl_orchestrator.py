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
    """A :class:`FetchError` on a discovered page is counted as failed."""
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
