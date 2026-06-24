"""Fixture-driven snapshot tests for the full pagetomd pipeline.

Each test runs the pipeline against a canned HTML fixture and asserts the
output matches a committed snapshot.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pagetomd import pipeline
from pagetomd.config import Config
from pagetomd.exceptions import ExtractionEmptyError
from pagetomd.fetcher import Fetcher

# Match the ``tool_version: …`` line in the YAML frontmatter and replace
# it with a fixed placeholder so the snapshot is stable across
# environments where ``pagetomd.__version__`` resolves differently
# (``0.0.0+unknown`` for unsynced editable installs vs.
# ``0.1.dev0+gXXXXXXX`` for VCS-derived builds, etc.).
_VERSION_RE = re.compile(r"^tool_version: .+$", re.MULTILINE)

# Loopback URL with a randomised port (e.g. ``http://127.0.0.1:50123``) —
# the local HTTP server fixture binds to an OS-assigned free port so two
# concurrent test runs cannot collide. Mask it so playwright snapshots
# captured against that server stay byte-stable.
_LOOPBACK_RE = re.compile(r"http://127\.0\.0\.1:\d+")


def _mask(text: str) -> str:
    """Replace dynamic values (tool_version, loopback port) with stable placeholders."""
    masked = _VERSION_RE.sub("tool_version: <REDACTED>", text)
    masked = _LOOPBACK_RE.sub("http://127.0.0.1:<PORT>", masked)
    return masked


def deterministic_config(tmp_path: Path, **overrides: Any) -> Config:
    """Build a :class:`Config` whose output is byte-deterministic."""
    base: dict[str, Any] = {
        "url": "https://example.test/page",
        "output": tmp_path / "out.md",
        "no_fetched_at": True,
        "log_level": "error",
    }
    base.update(overrides)
    return Config.from_overrides(base)


def _run_and_read(
    tmp_path: Path,
    fetcher: Fetcher,
    **config_overrides: Any,
) -> str:
    """Run the full pipeline and return the masked output Markdown."""
    cfg = deterministic_config(tmp_path, **config_overrides)
    pipeline.run(cfg, fetcher=fetcher)
    output = (tmp_path / "out.md").read_text(encoding="utf-8")
    return _mask(output)


def test_blog(
    snapshot: Any,
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """A standard blog article round-trips into clean Markdown."""
    fetcher = fake_fetcher_factory(fixture_html("blog.html"))
    assert _run_and_read(tmp_path, fetcher) == snapshot


def test_docs(
    snapshot: Any,
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """Technical docs survive nested headings, inline code, and tables."""
    fetcher = fake_fetcher_factory(fixture_html("docs.html"))
    assert _run_and_read(tmp_path, fetcher) == snapshot


def test_github_readme(
    snapshot: Any,
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """A GitHub-style README keeps badges, TOC, and fenced code blocks."""
    fetcher = fake_fetcher_factory(fixture_html("github_readme.html"))
    assert _run_and_read(tmp_path, fetcher) == snapshot


def test_news(
    snapshot: Any,
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """Chrome (nav, cookie banner, related-articles) is stripped from a news page."""
    fetcher = fake_fetcher_factory(fixture_html("news.html"))
    output = _run_and_read(tmp_path, fetcher)
    # Defence-in-depth: independent of snapshot wiring, the cookie banner
    # text must never leak into the rendered Markdown body.
    assert "cookies to improve your experience" not in output
    assert output == snapshot


def test_cross_reference_links(
    snapshot: Any,
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """Documentation-portal cross-reference patterns render correctly.

    Locks in the link-preservation behaviour for nested-markup cross
    references emitted by documentation portals:

    - Pattern A (orphan bare anchor inside ``<li><p>…see <a>…</a>.</p></li>``)
      renders on a single bullet line, not split into two blocks.
    - Pattern B (``<a><span class='xreftitle'>…</span></a>``) renders as a
      clean Markdown link with no leaked ``xreftitle`` markup.
    - Pattern C (cross-reference prose with no ``<a>`` in source) passes
      through unchanged — no fake link is injected.
    """
    fetcher = fake_fetcher_factory(fixture_html("cross_reference_links.html"))
    output = _run_and_read(tmp_path, fetcher)

    # Defence-in-depth invariants, independent of the snapshot wiring.
    expected_pattern_a = (
        "Identity Engine must be set up. For more information, see [Identity Engine Setup]"
    )
    assert expected_pattern_a in output, (
        "Pattern A link must render inline on its bullet, not split into a "
        f"separate block. Got:\n{output}"
    )
    assert "[Assistant role-based access control](" in output, (
        f"Pattern B xref link missing from rendered Markdown:\n{output}"
    )
    assert "xreftitle" not in output, (
        f"Decorative ``xreftitle`` class leaked into Markdown output:\n{output}"
    )

    assert output == snapshot


def test_spa_vue_httpx_raises_empty(
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """SPA shell with empty ``#app`` raises :class:`ExtractionEmptyError`.

    Static fetch sees only the empty mount point; the contract is to raise
    so ``auto``/``playwright`` modes can recover.
    """
    fetcher = fake_fetcher_factory(fixture_html("spa_vue.html"))
    cfg = deterministic_config(tmp_path)
    with pytest.raises(ExtractionEmptyError):
        pipeline.run(cfg, fetcher=fetcher)


@pytest.mark.playwright
def test_spa_vue_playwright_extracts(
    tmp_path: Path,
    chromium_available: bool,
    local_http_server: str,
    snapshot: Any,
) -> None:
    """``--fetcher playwright`` renders the SPA fixture and extracts the article."""
    if not chromium_available:
        pytest.skip("chromium not available; run `playwright install chromium`")
    cfg = deterministic_config(
        tmp_path,
        url=f"{local_http_server}/spa_vue.html",
        fetcher="playwright",
    )
    pipeline.run(cfg)
    output = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert _mask(output) == snapshot


def test_code_heavy(
    snapshot: Any,
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """Five fenced blocks in five languages survive end-to-end conversion."""
    fetcher = fake_fetcher_factory(fixture_html("code_heavy.html"))
    assert _run_and_read(tmp_path, fetcher) == snapshot


@pytest.mark.parametrize("wide_mode", ["kv", "html", "drop"])
def test_tables(
    wide_mode: str,
    snapshot: Any,
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """Each wide-table strategy produces a distinct, stable rendering."""
    fetcher = fake_fetcher_factory(fixture_html("tables.html"))
    assert _run_and_read(tmp_path, fetcher, wide_tables=wide_mode) == snapshot


def test_rtl_hebrew(
    snapshot: Any,
    tmp_path: Path,
    fixture_html: Callable[[str], str],
    fake_fetcher_factory: Callable[..., Fetcher],
) -> None:
    """Hebrew text round-trips through UTF-8 + NFC without mojibake."""
    fetcher = fake_fetcher_factory(fixture_html("rtl_hebrew.html"))
    output = _run_and_read(tmp_path, fetcher)
    # Independent of snapshot wiring, the rendered Markdown must contain
    # at least one Hebrew codepoint — i.e. the text was not byte-escaped
    # or transliterated somewhere in the pipeline.
    assert any("\u0590" <= ch <= "\u05ff" for ch in output), (
        "Expected at least one Hebrew codepoint in the rendered output"
    )
    assert output == snapshot
