"""Unit tests for :mod:`pagetomd.pipeline` using fake fetchers (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from pagetomd.config import Config
from pagetomd.exceptions import (
    ConversionError,
    DependencyMissingError,
    ExtractionEmptyError,
    FetchError,
    PageToMdError,
    RobotsDisallowedError,
)
from pagetomd.fetcher import FetchedDoc
from pagetomd.pipeline import PipelineResult, run


_BODY_TEMPLATE = (
    "This is meaningful article body text marker-{marker} that exists to "
    "give the extractor enough material to identify as the main article "
    "body. We pad it so trafilatura's recall heuristics latch onto it for "
    "end-to-end tests, and we vary the {marker} per test so the LRU "
    "deduplicator inside trafilatura never short-circuits us."
)


def _article_html(marker: str) -> str:
    """Build article-shaped HTML whose body is unique per ``marker``."""
    body = _BODY_TEMPLATE.format(marker=marker)
    return (
        "<html><head><title>Article Title</title></head>"
        f"<body><article><h1>Article Title</h1><p>{body}</p></article></body></html>"
    )


_EMPTY_HTML = "<!doctype html><html><head></head><body></body></html>"


def _doc(
    *,
    marker: str,
    html: str | None = None,
    url: str = "https://example.com/x",
) -> FetchedDoc:
    """Build a :class:`FetchedDoc` populated with a deterministic payload."""
    return FetchedDoc(
        url=url,
        final_url=url,
        status_code=200,
        html=html if html is not None else _article_html(marker),
        content_type="text/html; charset=utf-8",
        encoding="utf-8",
        headers={},
        elapsed_ms=12,
    )


class FakeFetcher:
    """Fake fetcher that returns a seeded doc or raises a seeded exception."""

    def __init__(
        self,
        *,
        doc: FetchedDoc | None = None,
        exc: Exception | None = None,
        marker: str = "default",
    ) -> None:
        self._doc = doc if doc is not None else _doc(marker=marker)
        self._exc = exc
        self.calls: list[str] = []
        self.closed = False

    def fetch(self, url: str) -> FetchedDoc:
        """Return the seeded doc or raise the seeded exception."""
        self.calls.append(url)
        if self._exc is not None:
            raise self._exc
        return self._doc

    def close(self) -> None:
        """Mark the fake as closed (the pipeline should never call this)."""
        self.closed = True


class ContextManagerFakeFetcher(FakeFetcher):
    """FakeFetcher that also tracks ``__enter__``/``__exit__`` calls."""

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.entered = False
        self.exited = False

    def __enter__(self) -> ContextManagerFakeFetcher:
        self.entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        self.exited = True


def make_config(tmp_path: Path, **overrides: object) -> Config:
    """Build a :class:`Config` with safe defaults for pipeline tests."""
    base: dict[str, object] = {
        "url": "https://example.com/x",
        "output": tmp_path / "out.md",
        "log_level": "warning",
    }
    base.update(overrides)
    return Config.from_overrides(base)


@pytest.fixture(autouse=True)
def _reset_contextvars() -> Iterator[None]:
    """Guarantee no test inherits or leaks structlog contextvars."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# No global logging config here: structlog's cache_logger_on_first_use
# would poison cross-module test isolation once any cached logger is touched.


def test_run_happy_path_writes_file(tmp_path: Path) -> None:
    """Full pipeline writes a file beginning with frontmatter + body text."""
    config = make_config(tmp_path)
    fetcher = FakeFetcher(marker="happy")

    result = run(config, fetcher=fetcher)

    assert isinstance(result, PipelineResult)
    assert result.output_path is not None
    assert result.output_path.exists()
    text = result.output_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "Article Title" in text
    assert "meaningful article body text" in text
    assert fetcher.calls == [config.url]


def test_run_result_fields_populated(tmp_path: Path) -> None:
    """``PipelineResult`` carries the expected per-field values."""
    target = tmp_path / "out.md"
    config = make_config(tmp_path, output=target)
    fetcher = FakeFetcher(marker="result-fields")

    result = run(config, fetcher=fetcher)

    assert result.output_path == target
    assert result.bytes_written > 0
    assert result.bytes_written == len(target.read_bytes())
    assert result.final_url == "https://example.com/x"
    assert result.title == "Article Title"
    assert result.elapsed_ms >= 0


def test_run_stdout_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``output == Path("-")`` streams to stdout and creates no file."""
    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path, output=Path("-"))
    fetcher = FakeFetcher(marker="stdout-sink")

    result = run(config, fetcher=fetcher)

    captured = capsys.readouterr()
    assert result.output_path is None
    # structlog may prepend to stdout, so check content via `in`.
    assert "---\n" in captured.out
    assert "Article Title" in captured.out
    assert "url: https://example.com/x" in captured.out
    assert list(tmp_path.iterdir()) == []


def test_run_default_output_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``output is None`` derives the path from :func:`slugify_default_path`."""
    from pagetomd.extractor import extract as _extract_impl
    from pagetomd.writer import slugify_default_path

    monkeypatch.chdir(tmp_path)
    config = make_config(tmp_path, output=None)
    fetcher = FakeFetcher(marker="default-path")

    expected = slugify_default_path(
        fetcher._doc,
        _extract_impl(fetcher._doc, config),
    )

    result = run(config, fetcher=fetcher)

    assert result.output_path is not None
    assert result.output_path.name == expected.name
    assert result.output_path.exists()


def test_run_no_fetched_at_true_omits_timestamp(tmp_path: Path) -> None:
    """``no_fetched_at=True`` drops the ``fetched_at:`` frontmatter field."""
    config = make_config(tmp_path, no_fetched_at=True)
    fetcher = FakeFetcher(marker="no-fetched-at-true")

    result = run(config, fetcher=fetcher)

    assert result.output_path is not None
    assert "fetched_at:" not in result.output_path.read_text(encoding="utf-8")


def test_run_no_fetched_at_default_includes_timestamp(tmp_path: Path) -> None:
    """The default (``no_fetched_at=False``) preserves ``fetched_at:``."""
    config = make_config(tmp_path)  # default no_fetched_at == False
    fetcher = FakeFetcher(marker="no-fetched-at-default")

    result = run(config, fetcher=fetcher)

    assert result.output_path is not None
    assert "fetched_at:" in result.output_path.read_text(encoding="utf-8")


def test_run_fetch_error_surfaces_unwrapped(tmp_path: Path) -> None:
    """``FetchError`` raised by the fetcher propagates without wrapping."""
    target = tmp_path / "out.md"
    config = make_config(tmp_path, output=target)
    fetcher = FakeFetcher(exc=FetchError("bad", url=config.url))

    with pytest.raises(FetchError) as excinfo:
        run(config, fetcher=fetcher)

    assert "bad" in str(excinfo.value)
    assert not target.exists()


def test_run_robots_disallowed_surfaces_unwrapped(tmp_path: Path) -> None:
    """``RobotsDisallowedError`` propagates unchanged."""
    config = make_config(tmp_path)
    fetcher = FakeFetcher(exc=RobotsDisallowedError("nope", url=config.url))

    with pytest.raises(RobotsDisallowedError):
        run(config, fetcher=fetcher)


def test_run_extraction_empty_surfaces_unwrapped(tmp_path: Path) -> None:
    """An empty body triggers :class:`ExtractionEmptyError` from the extractor."""
    config = make_config(tmp_path)
    fetcher = FakeFetcher(doc=_doc(marker="empty", html=_EMPTY_HTML))

    with pytest.raises(ExtractionEmptyError):
        run(config, fetcher=fetcher)


def test_run_conversion_error_surfaces_unwrapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A :class:`ConversionError` raised mid-pipeline bubbles up unchanged."""
    config = make_config(tmp_path)
    fetcher = FakeFetcher(marker="conv-err")

    def _raise_conversion(*_args: object, **_kwargs: object) -> str:
        raise ConversionError("conv kaboom")

    monkeypatch.setattr("pagetomd.pipeline.convert", _raise_conversion)

    with pytest.raises(ConversionError) as excinfo:
        run(config, fetcher=fetcher)
    assert "conv kaboom" in str(excinfo.value)


def test_run_unexpected_exception_wrapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare :class:`RuntimeError` is wrapped as :class:`PageToMdError`."""
    config = make_config(tmp_path)
    fetcher = FakeFetcher(marker="boom")

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("pagetomd.pipeline.convert", _boom)

    with pytest.raises(PageToMdError) as excinfo:
        run(config, fetcher=fetcher)

    # The original exception must be preserved as the cause AND surfaced
    # via the structured context["original"] for log/debug readability.
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert excinfo.value.context.get("original") == "boom"


def test_run_playwright_missing_dependency_raises_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetcher='playwright'`` without the extra installed raises typed.

    Inserts ``None`` sentinels into ``sys.modules`` to simulate a
    missing optional dependency.
    """
    import sys

    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    config = make_config(tmp_path, fetcher="playwright")

    with pytest.raises(DependencyMissingError) as excinfo:
        run(config)

    assert "playwright" in str(excinfo.value).lower()


def test_run_injected_fetcher_lifecycle_untouched(tmp_path: Path) -> None:
    """The pipeline never tears down a caller-injected fetcher."""
    config = make_config(tmp_path)
    fetcher = FakeFetcher(marker="lifecycle")

    run(config, fetcher=fetcher)

    assert fetcher.closed is False


def test_run_httpx_fetcher_context_managed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no fetcher is injected, the httpx backend is entered + exited."""
    config = make_config(tmp_path, fetcher="httpx")

    created: list[ContextManagerFakeFetcher] = []

    def _factory(cfg: Config) -> ContextManagerFakeFetcher:
        instance = ContextManagerFakeFetcher(cfg)
        created.append(instance)
        return instance

    monkeypatch.setattr("pagetomd.pipeline.HttpxFetcher", _factory)

    run(config)

    assert len(created) == 1
    assert created[0].entered is True
    assert created[0].exited is True


def test_run_clears_contextvars_on_success(tmp_path: Path) -> None:
    """A successful run must leave the contextvar store empty."""
    config = make_config(tmp_path)
    fetcher = FakeFetcher(marker="ctxvars-ok")

    run(config, fetcher=fetcher)

    assert structlog.contextvars.get_contextvars() == {}


def test_run_clears_contextvars_on_failure(tmp_path: Path) -> None:
    """A failed run must still leave the contextvar store empty."""
    config = make_config(tmp_path)
    fetcher = FakeFetcher(exc=FetchError("nope", url=config.url))

    with pytest.raises(FetchError):
        run(config, fetcher=fetcher)

    assert structlog.contextvars.get_contextvars() == {}


def test_run_emits_pipeline_start_and_ok(tmp_path: Path) -> None:
    """Both ``pipeline.start`` and ``pipeline.ok`` appear with expected fields."""
    config = make_config(tmp_path, fetcher="httpx")
    fetcher = FakeFetcher(marker="log-events")

    with capture_logs() as cap:
        result = run(config, fetcher=fetcher)

    events = {entry["event"]: entry for entry in cap}
    assert "pipeline.start" in events
    assert "pipeline.ok" in events

    start = events["pipeline.start"]
    assert start["fetcher"] == "httpx"
    assert start["output"].endswith("out.md")

    ok = events["pipeline.ok"]
    assert ok["elapsed_ms"] == result.elapsed_ms
    assert ok["bytes_written"] == result.bytes_written
    assert ok["output_path"].endswith("out.md")


def test_resolve_base_url_falls_back_to_final_url_when_no_base_href() -> None:
    """``base_href=None`` → the fetcher's ``final_url`` wins."""
    from pagetomd.pipeline import _resolve_base_url

    assert (
        _resolve_base_url(base_href=None, final_url="https://example.com/x")
        == "https://example.com/x"
    )


def test_resolve_base_url_resolves_relative_base_href_against_final_url() -> None:
    """A relative ``<base href>`` is resolved against ``final_url``."""
    from pagetomd.pipeline import _resolve_base_url

    assert (
        _resolve_base_url(base_href="/assets/", final_url="https://example.com/post")
        == "https://example.com/assets/"
    )


def test_resolve_base_url_uses_absolute_base_href_directly() -> None:
    """An absolute ``<base href>`` wins outright."""
    from pagetomd.pipeline import _resolve_base_url

    assert (
        _resolve_base_url(
            base_href="https://cdn.example.com/site/",
            final_url="https://origin.example.test/page",
        )
        == "https://cdn.example.com/site/"
    )
