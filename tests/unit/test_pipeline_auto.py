"""Unit tests for the ``auto`` fetcher mode and its SPA fallback heuristic."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from pagetomd import pipeline
from pagetomd.config import Config
from pagetomd.exceptions import ExtractionEmptyError, FetchError
from pagetomd.fetcher import FetchedDoc
from pagetomd.pipeline import run
from tests.conftest import make_config, make_fetched_doc


_RICH_BODY_TEXT = (
    "This is a real article with plenty of meaningful body text. "
    "It contains many sentences, none of which are SPA placeholders. "
    "Every reader should agree that the static HTML carried the article. "
    "We deliberately pad it well past the 200 character SPA threshold "
    "so the heuristic recognises it as fully-rendered prose, not a shell."
)


def _shell_html(marker: str = '<div id="app"></div>') -> str:
    """Build a tiny SPA shell HTML body containing ``marker``."""
    return f"<html><head><title>x</title></head><body>{marker}</body></html>"


def _rich_html(marker: str | None = None) -> str:
    """Build an HTML body with rich text, optionally embedding ``marker``."""
    middle = marker or ""
    return (
        "<html><head><title>x</title></head>"
        f"<body><article><h1>Title</h1><p>{_RICH_BODY_TEXT}</p>{middle}</article></body></html>"
    )


class _FakeHttpx:
    """Stand-in for :class:`HttpxFetcher` with controllable behaviour."""

    def __init__(
        self,
        *,
        doc: FetchedDoc | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._doc = doc
        self._exc = exc
        self.calls: list[str] = []
        self.entered = False
        self.exited = False
        self.closed = False

    def __enter__(self) -> _FakeHttpx:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        self.exited = True

    def fetch(self, url: str) -> FetchedDoc:
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        assert self._doc is not None
        return self._doc

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    """Stand-in for :class:`PlaywrightFetcher` that records invocations."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.calls: list[str] = []
        self.called = False
        self.closed = False
        # Returned by ``fetch`` so the caller can assert it propagates.
        self._doc = make_fetched_doc(
            "<html><body><article><h1>Playwright</h1>"
            f"<p>{_RICH_BODY_TEXT}</p></article></body></html>",
            url="https://example.com/x",
        )

    def fetch(self, url: str) -> FetchedDoc:
        self.called = True
        self.calls.append(url)
        return self._doc

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        # Rich body wins even when an SPA marker is present.
        (_rich_html('<div id="app">x</div>'), False),
        # Tiny body + recognised SPA marker → True.
        (_shell_html('<div id="app"></div>'), True),
        # Tiny body but no marker → False (probably an empty / 404 page).
        ("<html><body><p>hi</p></body></html>", False),
        # Vue 3 SSR marker.
        (_shell_html('<div data-vue-app="1"></div>'), True),
        # React root marker.
        (_shell_html("<div data-reactroot></div>"), True),
        # Angular marker.
        (_shell_html("<my-app ng-version='17.0.0'></my-app>"), True),
        # Next.js mount.
        (_shell_html('<div id="__next"></div>'), True),
        # Nuxt mount.
        (_shell_html('<div id="__nuxt"></div>'), True),
        # noscript JS-required hint.
        (
            "<html><body><noscript>You need to enable JavaScript</noscript></body></html>",
            True,
        ),
        # Empty input is a no-op.
        ("", False),
    ],
)
def test_should_fallback_truth_table(html: str, expected: bool) -> None:
    """Heuristic returns True only when body is sparse AND a marker fires."""
    assert pipeline._should_fallback_to_playwright(html) is expected


def test_auto_skips_playwright_when_body_rich(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rich httpx response short-circuits — playwright is never built."""
    config = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="auto",
    )
    fake_httpx = _FakeHttpx(doc=make_fetched_doc(_rich_html()))
    monkeypatch.setattr(pipeline, "HttpxFetcher", lambda cfg: fake_httpx)
    monkeypatch.setattr(pipeline, "PlaywrightFetcher", _FakePlaywright)

    with pipeline._AutoFetcher(config) as auto:
        result = auto.fetch(config.url)

    assert result.html == _rich_html()
    assert fake_httpx.calls == [config.url]
    # _FakePlaywright was never instantiated.
    assert auto._playwright is None


def test_auto_falls_back_on_spa_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SPA-shaped httpx response triggers the playwright retry."""
    config = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="auto",
    )
    shell = _shell_html('<div id="app"></div>')
    fake_httpx = _FakeHttpx(doc=make_fetched_doc(shell))
    monkeypatch.setattr(pipeline, "HttpxFetcher", lambda cfg: fake_httpx)
    monkeypatch.setattr(pipeline, "PlaywrightFetcher", _FakePlaywright)

    # Capture references inside the ``with`` block — ``close()`` nulls the
    # backend handles on exit, so post-exit introspection would see None.
    captured_playwright: _FakePlaywright | None = None
    with pipeline._AutoFetcher(config) as auto:
        result = auto.fetch(config.url)
        captured_playwright = auto._playwright  # type: ignore[assignment]

    # Returned doc is the playwright one, not the original SPA shell.
    assert "Playwright" in result.html
    assert fake_httpx.calls == [config.url]
    assert isinstance(captured_playwright, _FakePlaywright)
    assert captured_playwright.called is True
    assert captured_playwright.calls == [config.url]
    # Lifecycle: ``__exit__`` closed both backends.
    assert fake_httpx.closed is True
    assert captured_playwright.closed is True


def test_auto_propagates_httpx_fetch_error_without_trying_playwright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Httpx-side errors bubble up untouched — no browser is launched."""
    config = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="auto",
    )
    boom = FetchError("network down", url=config.url)
    fake_httpx = _FakeHttpx(exc=boom)
    monkeypatch.setattr(pipeline, "HttpxFetcher", lambda cfg: fake_httpx)
    monkeypatch.setattr(pipeline, "PlaywrightFetcher", _FakePlaywright)

    with pipeline._AutoFetcher(config) as auto, pytest.raises(FetchError) as excinfo:
        auto.fetch(config.url)

    assert excinfo.value is boom
    assert auto._playwright is None


def test_auto_close_lifecycle_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both backends are closed on ``__exit__`` even when the body raised."""
    config = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="auto",
    )
    shell = _shell_html('<div id="app"></div>')
    fake_httpx = _FakeHttpx(doc=make_fetched_doc(shell))
    monkeypatch.setattr(pipeline, "HttpxFetcher", lambda cfg: fake_httpx)
    monkeypatch.setattr(pipeline, "PlaywrightFetcher", _FakePlaywright)

    captured_playwright: _FakePlaywright | None = None
    with pytest.raises(RuntimeError, match="boom"), pipeline._AutoFetcher(config) as auto:
        # Trigger the playwright lazy-build so close() has both to tear down.
        auto.fetch(config.url)
        captured_playwright = auto._playwright  # type: ignore[assignment]
        raise RuntimeError("boom")

    assert fake_httpx.closed is True
    assert isinstance(captured_playwright, _FakePlaywright)
    assert captured_playwright.closed is True


def test_auto_close_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling close() twice is safe and does not double-close backends."""
    config = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="auto",
    )
    fake_httpx = _FakeHttpx(doc=make_fetched_doc(_rich_html()))
    monkeypatch.setattr(pipeline, "HttpxFetcher", lambda cfg: fake_httpx)
    monkeypatch.setattr(pipeline, "PlaywrightFetcher", _FakePlaywright)

    auto = pipeline._AutoFetcher(config)
    with auto:
        pass  # __exit__ calls close() once

    assert fake_httpx.closed is True
    auto.close()
    assert fake_httpx.closed is True


def test_select_fetcher_httpx(tmp_path: Path) -> None:
    """``fetcher='httpx'`` selects :class:`HttpxFetcher`."""
    from pagetomd.fetcher import HttpxFetcher

    cfg = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="httpx",
    )
    selected = pipeline._select_fetcher(cfg)
    assert isinstance(selected, HttpxFetcher)


def test_select_fetcher_auto(tmp_path: Path) -> None:
    """``fetcher='auto'`` selects :class:`_AutoFetcher`."""
    cfg = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="auto",
    )
    selected = pipeline._select_fetcher(cfg)
    assert isinstance(selected, pipeline._AutoFetcher)


# ---------------------------------------------------------------------------
# Extraction-failure fallback — pipeline retries with playwright when
# httpx content passes the SPA heuristic but extraction still fails.
# ---------------------------------------------------------------------------

# Unique body text so trafilatura's LRU deduplicator never short-circuits.
_EXTRACTION_FALLBACK_BODY = (
    "This is a real article with plenty of meaningful body text for the "
    "extraction-fallback test. It contains many sentences, none of which "
    "are SPA placeholders. We deliberately pad it well past the 200 "
    "character SPA threshold so the heuristic recognises it as fully-"
    "rendered prose, not a shell. Unique marker: extraction-fallback-pw."
)


def _playwright_article_html() -> str:
    """Rich article HTML that Playwright would return after rendering."""
    return (
        "<html><head><title>Article</title></head>"
        f"<body><article><h1>Article</h1><p>{_EXTRACTION_FALLBACK_BODY}</p></article></body></html>"
    )


def test_auto_pipeline_falls_back_on_extraction_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When httpx HTML passes heuristic but extraction fails, retry with playwright."""
    config = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="auto",
    )
    # httpx returns a rich-looking page (passes SPA heuristic) but
    # extraction will be forced to fail via a mock.
    httpx_doc = make_fetched_doc(_rich_html(), url="https://example.com/x")
    fake_httpx = _FakeHttpx(doc=httpx_doc)
    monkeypatch.setattr(pipeline, "HttpxFetcher", lambda cfg: fake_httpx)

    # Override the playwright fake's response to carry extractable content.
    original_init = _FakePlaywright.__init__

    def _init_with_article(self: _FakePlaywright, cfg: Config) -> None:
        original_init(self, cfg)
        self._doc = make_fetched_doc(
            _playwright_article_html(), url="https://example.com/x"
        )

    monkeypatch.setattr(_FakePlaywright, "__init__", _init_with_article)
    monkeypatch.setattr(pipeline, "PlaywrightFetcher", _FakePlaywright)

    # Mock extract: fail on the first call (httpx HTML), succeed on the second
    # (playwright HTML). This simulates a page whose static HTML cannot be
    # extracted but whose JS-rendered content can.
    call_count = 0
    real_extract = pipeline.extract

    def _extract_fail_then_succeed(doc: FetchedDoc, cfg: Config) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ExtractionEmptyError(
                "Extractor produced no readable content",
                url="https://example.com/x",
                html_length=len(doc.html),
            )
        return real_extract(doc, cfg)

    monkeypatch.setattr(pipeline, "extract", _extract_fail_then_succeed)

    result = run(config)

    assert call_count == 2
    assert result.output_path is not None
    assert result.output_path.exists()
    text = result.output_path.read_text(encoding="utf-8")
    assert "extraction-fallback-pw" in text


def test_auto_pipeline_extraction_empty_no_fallback_for_non_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When fetcher is not auto, ExtractionEmptyError propagates unchanged."""
    config = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="httpx",
    )

    # Force extraction to always fail.
    def _extract_always_fail(doc: FetchedDoc, cfg: Config) -> object:
        raise ExtractionEmptyError(
            "Extractor produced no readable content",
            url="https://example.com/x",
            html_length=len(doc.html),
        )

    monkeypatch.setattr(pipeline, "extract", _extract_always_fail)

    fetcher_doc = make_fetched_doc(_rich_html(), url="https://example.com/x")
    fake_httpx = _FakeHttpx(doc=fetcher_doc)
    monkeypatch.setattr(pipeline, "HttpxFetcher", lambda cfg: fake_httpx)

    with pytest.raises(ExtractionEmptyError):
        run(config)


def test_auto_fetch_playwright_method(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch_playwright`` lazily builds and delegates to the playwright backend."""
    config = make_config(
        url="https://example.com/x",
        output=tmp_path / "out.md",
        log_level="warning",
        fetcher="auto",
    )
    fake_httpx = _FakeHttpx(doc=make_fetched_doc(_rich_html()))
    monkeypatch.setattr(pipeline, "HttpxFetcher", lambda cfg: fake_httpx)
    monkeypatch.setattr(pipeline, "PlaywrightFetcher", _FakePlaywright)

    with pipeline._AutoFetcher(config) as auto:
        assert auto._playwright is None
        result = auto.fetch_playwright(config.url)

    assert "Playwright" in result.html
