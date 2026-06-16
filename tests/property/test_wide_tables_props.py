"""Property-based tests for wide-table rendering across the ``kv``, ``html``, and ``drop`` modes."""

from __future__ import annotations

import re
from typing import Final

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings

from pagetomd.config import Config
from pagetomd.converter import convert

pytestmark = pytest.mark.property


# Cell text alphabet kept narrow so the rendered bullets / table cells
# don't accidentally trip Markdown special characters (``*``, ``|``,
# ``[``, ``(``) — the properties are about *count*, not content.
_CELL_ALPHABET: Final[str] = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "

# Single bullet line in the kv-mode output:
#   "- **<header>**: <value>"
# The bold-wrapped key + ``: `` separator is the unambiguous marker
# (vs. accidental ``- `` lines elsewhere in the output).
_BULLET_RE: Final[re.Pattern[str]] = re.compile(r"^- \*\*[^*]+\*\*: ", re.MULTILINE)


def _cfg(wide_tables: str) -> Config:
    """Build a minimal :class:`Config` selecting the requested wide-table mode."""
    return Config(  # type: ignore[call-arg]
        url="https://example.com",
        wide_tables=wide_tables,  # type: ignore[arg-type]
    )


_CELL_TEXT: st.SearchStrategy[str] = (
    st.text(
        alphabet=_CELL_ALPHABET,
        min_size=1,
        max_size=12,
    )
    .map(str.strip)
    .filter(lambda s: len(s) > 0)
)


@st.composite
def tei_table(draw: st.DrawFn) -> tuple[str, int, int]:
    """Generate a TEI-shaped ``<table>``; returns ``(html, cols, rows)``."""
    cols = draw(st.integers(min_value=6, max_value=20))
    rows = draw(st.integers(min_value=2, max_value=10))

    parts: list[str] = ["<table>"]
    # Header row: one ``<row>`` of ``<cell role="head">`` entries.
    parts.append("<row>")
    for _ in range(cols):
        text = draw(_CELL_TEXT)
        parts.append(f'<cell role="head">{text}</cell>')
    parts.append("</row>")

    # Data rows: ``<row>`` of plain ``<cell>`` entries.
    for _ in range(rows - 1):
        parts.append("<row>")
        for _ in range(cols):
            text = draw(_CELL_TEXT)
            parts.append(f"<cell>{text}</cell>")
        parts.append("</row>")

    parts.append("</table>")
    return "".join(parts), cols, rows


PROPERTY_SETTINGS = settings(
    max_examples=200,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)


@PROPERTY_SETTINGS
@given(tei_table())
def test_kv_mode_emits_exact_bullet_count(case: tuple[str, int, int]) -> None:
    """``kv`` mode emits ``(rows - 1) * cols`` bullets total."""
    html, cols, rows = case
    md = convert(html, _cfg("kv"))
    bullet_count = len(_BULLET_RE.findall(md))
    expected = (rows - 1) * cols
    assert bullet_count == expected, (
        f"expected {expected} bullets for {rows=} {cols=}, got {bullet_count}\noutput:\n{md}"
    )


@PROPERTY_SETTINGS
@given(tei_table())
def test_drop_mode_emits_single_drop_comment(case: tuple[str, int, int]) -> None:
    """``drop`` mode emits exactly one ``pagetomd: wide table dropped`` comment."""
    html, _cols, _rows = case
    md = convert(html, _cfg("drop"))
    count = md.count("pagetomd: wide table dropped")
    assert count == 1, f"expected one drop marker, got {count}\noutput:\n{md}"


@PROPERTY_SETTINGS
@given(tei_table())
def test_html_mode_strips_tei_markup(case: tuple[str, int, int]) -> None:
    """``html`` mode never leaks raw TEI ``<row>`` / ``<cell>`` tokens."""
    html, _cols, _rows = case
    md = convert(html, _cfg("html"))
    lowered = md.lower()
    assert "<row" not in lowered, f"<row> leaked into output:\n{md}"
    assert "<cell" not in lowered, f"<cell> leaked into output:\n{md}"
