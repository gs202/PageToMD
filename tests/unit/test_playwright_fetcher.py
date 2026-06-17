"""Unit tests for :class:`pagetomd.fetcher.PlaywrightFetcher`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pagetomd import fetcher as fetcher_module
from pagetomd.exceptions import DependencyMissingError
from tests.conftest import make_config


def test_all_exports_playwright_fetcher() -> None:
    """``PlaywrightFetcher`` is re-exported from ``pagetomd.fetcher.__all__``."""
    assert "PlaywrightFetcher" in fetcher_module.__all__


def test_dependency_missing_when_playwright_not_importable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Construction raises :class:`DependencyMissingError` when import fails."""
    cfg = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        playwright_idle_ms=0,
    )
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    with pytest.raises(DependencyMissingError) as excinfo:
        fetcher_module.PlaywrightFetcher(cfg)

    assert "playwright" in str(excinfo.value).lower()
    # Hint points to both the extra install AND the chromium download.
    assert "chromium" in str(excinfo.value).lower()


@pytest.mark.playwright
def test_playwright_smoke_renders_local_fixture(
    chromium_available: bool,
    local_http_server: str,
    tmp_path: Path,
) -> None:
    """Headless Chromium renders ``spa_vue.html`` and returns the hydrated DOM."""
    if not chromium_available:
        pytest.skip("chromium not available; run `playwright install chromium`")

    cfg = make_config(
        url=f"{local_http_server}/spa_vue.html",
        output=tmp_path / "out.md",
        log_level="warning",
        playwright_idle_ms=0,
    )
    with fetcher_module.PlaywrightFetcher(cfg) as f:
        doc = f.fetch(cfg.url)

    assert doc.status_code == 200
    assert doc.final_url.endswith("/spa_vue.html")
    # The hydration script injects the literal article H1.
    assert "Understanding Reactive State" in doc.html


@pytest.mark.playwright
def test_playwright_robots_delegate_is_invoked(
    chromium_available: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For public hosts the fetcher consults the httpx-based robots check."""
    if not chromium_available:
        pytest.skip("chromium not available; run `playwright install chromium`")

    cfg = make_config(
        url="https://example.com/x",  # public host → robots check runs
        output=tmp_path / "out.md",
        log_level="warning",
        playwright_idle_ms=0,
        respect_robots=True,
    )
    calls: list[str] = []

    pf = fetcher_module.PlaywrightFetcher(cfg)

    def _record(client: object, parsed: object, bound: object) -> None:
        calls.append(getattr(parsed, "raw", "?"))

    monkeypatch.setattr(pf._robots_delegate, "_check_robots", _record)

    # Re-route navigation so we never hit the public internet from a test.
    def _no_render(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("render skipped — robots assertion is the test target")

    monkeypatch.setattr(pf, "_render", _no_render)

    with pytest.raises(RuntimeError, match="render skipped"):
        pf.fetch("https://example.com/x")

    assert calls == ["https://example.com/x"]
