"""Render every HTML fixture to Markdown for the markdownlint CI job.

This script is the bridge between the in-repo fixture corpus and the
:command:`markdownlint-cli2` linter. CI runs it once to materialise
``build/fixture_md/<name>.md`` files, then points the linter at the
glob ``build/fixture_md/*.md``. Locally, contributors can invoke it
the same way to reproduce the lint check:

.. code-block:: shell

    uv run python scripts/render_fixtures.py
    npx --yes markdownlint-cli2 'build/fixture_md/*.md'

The script intentionally short-circuits the network: it builds an
in-process :class:`~pagetomd.fetcher.Fetcher` fake that returns the
fixture HTML verbatim, then runs the full
:func:`pagetomd.pipeline.run` against each fixture. ``--no-fetched-at``
is implied by passing ``no_fetched_at=True`` so the output is byte-
deterministic and lint diffs are pure signal.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Allow ``python scripts/render_fixtures.py`` invocation from a checkout
# without ``pip install -e .``. ``uv run`` already wires the path
# correctly, but the manual invocation should not fail mysteriously.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from pagetomd.config import Config  # noqa: E402
from pagetomd.exceptions import ExtractionEmptyError  # noqa: E402
from pagetomd.fetcher import FetchedDoc, Fetcher  # noqa: E402
from pagetomd.pipeline import run  # noqa: E402

# Fixtures we intentionally skip because they're designed to fail the
# static extraction path. ``spa_vue.html`` is an SPA-shell fixture that
# exists solely to drive the Playwright auto-fallback test — feeding it
# through the static pipeline would (correctly) raise
# :class:`~pagetomd.exceptions.ExtractionEmptyError`. Listed here so the
# script never prints a misleading FAIL line.
_SKIP_FIXTURES: frozenset[str] = frozenset({"spa_vue.html"})

# Directories the script reads from and writes to.
FIXTURES_DIR: Path = _REPO_ROOT / "tests" / "fixtures" / "html"
OUTPUT_DIR: Path = _REPO_ROOT / "build" / "fixture_md"

# Synthetic base URL used when feeding a fixture into the pipeline. Kept
# stable so the rewritten absolute URLs in the rendered Markdown are
# deterministic across runs / machines.
BASE_URL = "https://fixtures.local"


@dataclass(frozen=True, slots=True)
class _FixtureFetcher:
    """Fake :class:`Fetcher` that returns one canned HTML payload.

    Implements only what :func:`pagetomd.pipeline.run` requires: a
    ``fetch(url)`` method returning a :class:`FetchedDoc`. The instance
    is created per fixture so the closure over ``html`` / ``url`` stays
    obvious at the call site.
    """

    html: str
    url: str

    def fetch(self, url: str) -> FetchedDoc:
        """Return the canned HTML wrapped in a :class:`FetchedDoc`."""
        headers: Mapping[str, str] = {"content-type": "text/html; charset=utf-8"}
        return FetchedDoc(
            url=url,
            final_url=self.url,
            status_code=200,
            html=self.html,
            content_type="text/html; charset=utf-8",
            encoding="utf-8",
            headers=headers,
            elapsed_ms=0,
        )


def _build_config(url: str, output: Path) -> Config:
    """Build a deterministic :class:`Config` for one fixture rendering.

    ``no_fetched_at=True`` strips the timestamp so the lint inputs are
    byte-stable across CI runs. ``respect_robots=False`` removes any
    network dependency a future robots-check refactor might introduce.
    """
    return Config(  # type: ignore[call-arg]
        url=url,
        output=output,
        overwrite=True,
        no_fetched_at=True,
        respect_robots=False,
        log_level="error",
    )


def _render_fixture(fixture: Path) -> Path:
    """Render ``fixture`` to ``build/fixture_md/<name>.md`` and return the path.

    Args:
        fixture: HTML fixture file under :data:`FIXTURES_DIR`.

    Returns:
        The destination Markdown file path.
    """
    html = fixture.read_text(encoding="utf-8")
    target = OUTPUT_DIR / f"{fixture.stem}.md"
    url = f"{BASE_URL}/{fixture.name}"
    cfg = _build_config(url=url, output=target)
    fetcher: Fetcher = _FixtureFetcher(html=html, url=url)
    run(cfg, fetcher=fetcher)
    return target


def main() -> int:
    """Render every fixture under :data:`FIXTURES_DIR` to :data:`OUTPUT_DIR`.

    Returns:
        ``0`` on success (every fixture rendered), ``1`` if any render
        raised an exception. The exception is printed to stderr so CI
        logs surface the cause.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fixtures = sorted(FIXTURES_DIR.glob("*.html"))
    if not fixtures:
        print(f"no fixtures found under {FIXTURES_DIR}", file=sys.stderr)
        return 1

    failures: list[tuple[str, str]] = []
    rendered = 0
    skipped = 0
    for fixture in fixtures:
        if fixture.name in _SKIP_FIXTURES:
            print(f"skip  {fixture.name} (intentional — see _SKIP_FIXTURES)")
            skipped += 1
            continue
        try:
            target = _render_fixture(fixture)
        except ExtractionEmptyError as exc:
            # An empty extraction on the static path is a real failure
            # for every fixture not in ``_SKIP_FIXTURES`` — surface it.
            failures.append((fixture.name, str(exc)))
            print(f"FAIL  {fixture.name}: {exc}", file=sys.stderr)
            continue
        except Exception as exc:
            failures.append((fixture.name, str(exc)))
            print(f"FAIL  {fixture.name}: {exc}", file=sys.stderr)
            continue
        rendered += 1
        print(f"ok    {fixture.name} → {target.relative_to(_REPO_ROOT)}")

    if failures:
        print(f"\n{len(failures)} fixture(s) failed to render", file=sys.stderr)
        return 1
    print(
        f"\nrendered {rendered} fixture(s) (skipped {skipped}) → "
        f"{OUTPUT_DIR.relative_to(_REPO_ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
