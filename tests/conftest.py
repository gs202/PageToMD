"""Project-wide pytest fixtures and per-module coverage gate."""

from __future__ import annotations

# SSRF guard bypass — tests hit loopback, so disable the private-address
# check. NEVER set this in production. Must precede pagetomd imports.
import asyncio
import os

os.environ.setdefault("PAGETOMD_INTERNAL_SKIP_SSRF", "1")

import functools
import pathlib
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pagetomd.fetcher import FetchedDoc, Fetcher

# Location of the hand-written HTML fixture corpus that drives the snapshot
# suite. Resolved once at import time so individual tests do not need to
# duplicate the path-construction logic.
FIXTURES_DIR: pathlib.Path = pathlib.Path(__file__).parent / "fixtures" / "pages"


@pytest.fixture
def fixture_html() -> Callable[[str], str]:
    """Return a loader that reads ``tests/fixtures/pages/{name}`` as UTF-8.

    The loader is deliberately minimal: it only resolves the path and reads
    the file. Callers that need to mutate the HTML before feeding it to the
    pipeline should do so after loading.

    Returns:
        A callable ``loader(name)`` that returns the UTF-8 decoded contents
        of the named fixture file. ``FileNotFoundError`` propagates from
        :meth:`pathlib.Path.read_text` if the file is missing.
    """

    def _load(name: str) -> str:
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")

    return _load


@dataclass(frozen=True, slots=True)
class _FakeFetcher:
    """In-memory :class:`Fetcher` that returns canned HTML for any URL."""

    html: str
    url: str
    content_type: str = "text/html; charset=utf-8"
    encoding: str = "utf-8"

    def fetch(self, url: str) -> FetchedDoc:
        """Return a :class:`FetchedDoc` populated from the canned HTML."""
        headers: Mapping[str, str] = {"content-type": self.content_type}
        return FetchedDoc(
            url=url,
            final_url=self.url,
            status_code=200,
            html=self.html,
            content_type=self.content_type,
            encoding=self.encoding,
            headers=headers,
            elapsed_ms=0,
        )


@pytest.fixture
def fake_fetcher_factory() -> Callable[..., Fetcher]:
    """Factory that builds a :class:`Fetcher` returning a fixed HTML payload."""

    def _make(html: str, url: str = "https://example.test/page") -> Fetcher:
        return _FakeFetcher(html=html, url=url)

    return _make


@pytest.fixture(scope="session")
def local_http_server() -> Iterator[str]:
    """Serve ``tests/fixtures/pages/`` on a random loopback port for the session."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _detach_event_loop_for_playwright(request: pytest.FixtureRequest) -> Iterator[None]:
    """Temporarily detach any running event loop for Playwright-marked tests.

    ``pytest-asyncio`` and ``anyio`` install a running event loop at session
    scope. Playwright's Sync API refuses to start when it detects one.
    This fixture unsets the running loop before each ``@pytest.mark.playwright``
    test and restores it afterwards.
    """
    marker = request.node.get_closest_marker("playwright")
    if marker is None:
        yield
        return

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Can't actually stop the loop, but we can hide it from the
        # current thread so Playwright's ``get_running_loop()`` raises.
        asyncio._set_running_loop(None)  # type: ignore[attr-defined]
        try:
            yield
        finally:
            asyncio._set_running_loop(loop)  # type: ignore[attr-defined]
    else:
        yield


@pytest.fixture(scope="session")
def chromium_available() -> bool:
    """Return ``True`` when ``playwright`` + chromium can launch headless."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    # Hide any running event loop so Playwright sync API can start.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        asyncio._set_running_loop(None)  # type: ignore[attr-defined]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                chromium_sandbox=not bool(os.environ.get("CI")),
            )
            browser.close()
        return True
    except Exception:
        return False
    finally:
        if loop is not None:
            asyncio._set_running_loop(loop)  # type: ignore[attr-defined]


_CRITICAL_MODULE_COVERAGE_THRESHOLDS: Mapping[str, float] = {
    "src/pagetomd/extractor.py": 90.0,
    "src/pagetomd/converter.py": 90.0,
    "src/pagetomd/writer.py": 90.0,
    "src/pagetomd/postprocess.py": 90.0,
}


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Enforce per-module coverage thresholds at session teardown.

    Skips silently when:
    - no ``.coverage`` file is present, or
    - only a subset of test markers was selected (``-m``).

    In CI the per-module gate runs as a standalone script inside the
    ``coverage`` job *after* all parallel test data has been combined.
    Running it inside each per-category job would always fail because
    a single category never covers the full codebase.
    """
    # If a marker filter was passed (``-m "unit and not playwright"`` etc.)
    # we are running a subset → skip the gate.
    if session.config.option.markexpr:
        return

    # No ``--cov`` was passed → ``.coverage`` was not written, nothing
    # to check.
    coverage_file = pathlib.Path(session.config.rootpath) / ".coverage"
    if not coverage_file.exists():
        return

    try:
        import coverage
    except ImportError:  # pragma: no cover - coverage is a dev dep
        return

    cov = coverage.Coverage(data_file=str(coverage_file))
    try:
        cov.load()
    except Exception:  # pragma: no cover - empty / corrupt data file
        return

    misses: list[str] = []
    for module_path, threshold in _CRITICAL_MODULE_COVERAGE_THRESHOLDS.items():
        pct = _module_combined_coverage(cov, module_path)
        if pct is None:
            misses.append(
                f"{module_path}: no coverage data recorded (was the test suite run with --cov?)"
            )
        elif pct < threshold:
            misses.append(f"{module_path}: {pct:.1f}% < {threshold:.0f}% required")

    if misses:
        joined = "\n  - ".join(misses)
        # Print and force a non-zero exit code. We avoid ``pytest.exit``
        # so the rest of the session's reporting (cov summary table) still
        # surfaces — the message lands above the standard footer.
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line("\nPer-module coverage gate FAILED:", red=True)
            terminal.write_line(f"  - {joined}", red=True)
        else:
            print("\nPer-module coverage gate FAILED:")
            print(f"  - {joined}")
        if session.exitstatus == 0:
            session.exitstatus = 1


def _module_combined_coverage(coverage_obj: object, module_path: str) -> float | None:
    """Return combined line+branch coverage percentage, or ``None`` if no data."""
    # ``_analyze`` is the internal-but-stable hook coverage.py uses to
    # build its terminal report rows. It returns an ``Analysis`` object
    # carrying both line and branch breakdowns, so the per-module gate
    # mirrors what users see at the bottom of ``pytest --cov``.
    try:
        analysis = coverage_obj._analyze(module_path)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - file missing from data
        return None

    total_lines = len(analysis.statements)
    covered_lines = total_lines - len(analysis.missing)

    total_branches = 0
    covered_branches = 0
    # coverage.py exposes ``has_arcs`` as a bool attribute on the
    # Analysis object (older versions had it as a method — be permissive
    # so the gate doesn't crash if a future release swaps shapes).
    has_arcs_attr = getattr(analysis, "has_arcs", False)
    has_arcs = has_arcs_attr() if callable(has_arcs_attr) else bool(has_arcs_attr)
    if has_arcs:
        try:
            branch_stats = analysis.branch_stats()
        except Exception:  # pragma: no cover - branch data unavailable
            branch_stats = {}
        for total, taken in branch_stats.values():
            total_branches += total
            covered_branches += taken

    total = total_lines + total_branches
    covered = covered_lines + covered_branches
    if total == 0:
        return None
    return covered / total * 100.0


# ---------------------------------------------------------------------------
# Shared test-data factories
# ---------------------------------------------------------------------------

from pagetomd.config import Config
from pagetomd.fetcher import FetchedDoc


def make_config(**overrides: object) -> Config:
    """Minimal :class:`Config` for unit tests; all fields overridable.

    Provides sensible defaults so callers only need to supply the fields
    relevant to the behaviour under test.
    """
    base: dict[str, object] = {
        "url": "https://example.com/",
        "timeout": 5.0,
        "retries": 3,
        "respect_robots": False,
        "follow_redirects": True,
        "max_redirects": 5,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def make_fetched_doc(
    html: str = "",
    url: str = "https://example.com/x",
    *,
    status_code: int = 200,
    content_type: str = "text/html; charset=utf-8",
    encoding: str = "utf-8",
    elapsed_ms: int = 1,
) -> FetchedDoc:
    """Minimal :class:`FetchedDoc` for unit tests; all fields overridable."""
    return FetchedDoc(
        url=url,
        final_url=url,
        status_code=status_code,
        html=html,
        content_type=content_type,
        encoding=encoding,
        headers={},
        elapsed_ms=elapsed_ms,
    )
