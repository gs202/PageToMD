"""Property-based tests for writer slugification and path-traversal safety."""

from __future__ import annotations

from urllib.parse import quote

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings

from pagetomd.extractor import ExtractedDoc
from pagetomd.writer import slugify_default_path
from tests.conftest import make_fetched_doc

pytestmark = pytest.mark.property


def _extracted(title: str | None) -> ExtractedDoc:
    """Minimal :class:`ExtractedDoc` carrying just the title the slugger reads."""
    return ExtractedDoc(
        title=title,
        author=None,
        date=None,
        description=None,
        site_name=None,
        language=None,
        cleaned_html="",
        base_href=None,
    )


# Any printable BMP text, excluding surrogates which would corrupt UTF-8.
# Includes path-separator-ish characters (``/``, ``\\``, ``..``) and NULs are
# excluded only by the ``min_codepoint=0x20`` floor (NUL would be filtered by
# slugify anyway, but we keep the strategy bounded to printable inputs).
_TITLES = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        min_codepoint=0x20,
        max_codepoint=0xFFFF,
    ),
    min_size=0,
    max_size=200,
)

# URL path segments restricted to URL-safe ASCII letters/digits. We percent-
# encode them so the resulting URL stays well-formed regardless of the
# generated content.
_URL_PATHS = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
    min_size=0,
    max_size=80,
).map(lambda s: f"https://example.com/{quote(s, safe='/')}")


@given(title=_TITLES, url=_URL_PATHS)
@settings(max_examples=200, deadline=2000)
def test_slug_never_escapes_cwd(title: str, url: str) -> None:
    """No matter the title or URL, the slug stays a single CWD-relative segment."""
    result = slugify_default_path(make_fetched_doc(url=url), _extracted(title))
    name = result.name
    # No embedded path separators of either flavour.
    assert "/" not in name
    assert "\\" not in name
    # No traversal segments — the writer must never hand back ``..`` or ``.``.
    assert name not in {".", ".."}
    assert not name.startswith("../")
    # Output is always a Markdown file.
    assert name.endswith(".md")
    # Single-segment relative path: ``Path("foo.md").parts == ("foo.md",)``.
    assert result.parts == (name,)


@given(
    reserved=st.sampled_from(
        ["CON", "PRN", "AUX", "NUL", "COM0", "COM1", "COM9", "LPT0", "LPT5", "LPT9"]
    )
)
def test_windows_reserved_stems_are_suffixed(reserved: str) -> None:
    """Windows reserved device names are appended with ``-page`` to avoid OS collision."""
    result = slugify_default_path(make_fetched_doc(url="https://x"), _extracted(reserved))
    stem = result.name.removesuffix(".md")
    forbidden_stems = (
        {"con", "prn", "aux", "nul"}
        | {f"com{i}" for i in range(10)}
        | {f"lpt{i}" for i in range(10)}
    )
    assert stem.lower() not in forbidden_stems
    assert stem.endswith("-page")
