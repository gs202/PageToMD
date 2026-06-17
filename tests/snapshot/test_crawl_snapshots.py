"""Snapshot tests guarding the byte-determinism of :func:`crawl` output."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx
from syrupy.assertion import SnapshotAssertion

from pagetomd.config import Config
from pagetomd.crawler import crawl

# ``tool_version`` resolves differently across environments (e.g.
# ``0.0.0+unknown`` for unsynced editable installs vs. VCS-derived builds),
# so mask it to keep the snapshot stable.
_VERSION_RE = re.compile(r"^tool_version: .+$", re.MULTILINE)

SEED_HTML = """<html><head><title>Docs Home</title></head><body>
<article>
<h1>Docs Home</h1>
<p>Welcome to the docs. This sentence pads the body so the extractor's recall
heuristics keep it; without enough material trafilatura gives up.</p>
<a href="/docs/home/intro">Intro</a>
</article>
</body></html>
"""

INTRO_HTML = """<html><head><title>Introduction</title></head><body>
<article>
<h1>Introduction</h1>
<p>Welcome to the docs. This is the introductory page with sufficient body
material to make trafilatura happy across multiple sentences and paragraphs.</p>
</article>
</body></html>
"""

_HTML_HEADERS = {"Content-Type": "text/html; charset=utf-8"}


def _mask(text: str) -> str:
    """Replace the dynamic ``tool_version`` line with a stable placeholder."""
    return _VERSION_RE.sub("tool_version: <REDACTED>", text)


@pytest.mark.snapshot
@respx.mock
def test_crawl_output_snapshot(tmp_path: Path, snapshot: SnapshotAssertion) -> None:
    """Crawled output is byte-identical across runs for fixed inputs.

    Output now mirrors the URL hierarchy under the seed (Option B):
    the seed page becomes ``index.md`` and discovered children live
    at paths matching their URL position.
    """
    respx.get("https://example.com/docs/home").mock(
        return_value=httpx.Response(200, html=SEED_HTML, headers=_HTML_HEADERS)
    )
    respx.get("https://example.com/docs/home/intro").mock(
        return_value=httpx.Response(200, html=INTRO_HTML, headers=_HTML_HEADERS)
    )

    cfg = Config(
        url="https://example.com/docs/home",
        output=tmp_path,
        respect_robots=False,
        no_fetched_at=True,
    )
    crawl(cfg, max_depth=1)

    # Use ``rglob`` (not ``glob``) because the output is no longer flat,
    # and key snapshots by relative path so file location is part of the
    # contract being guarded.
    for md_file in sorted(tmp_path.rglob("*.md")):
        relative = md_file.relative_to(tmp_path).as_posix()
        assert _mask(md_file.read_text(encoding="utf-8")) == snapshot(name=relative)
