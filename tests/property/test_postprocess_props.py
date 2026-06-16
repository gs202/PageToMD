"""Property-based tests for :func:`pagetomd.postprocess.postprocess`.

Asserts six invariants: idempotence, NFC stability, zero-width stripping,
trailing-newline shape, heading monotonicity, and fenced-code preservation.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from typing import Final

import hypothesis.strategies as st
import pytest
from hypothesis import HealthCheck, given, settings

from pagetomd.postprocess import postprocess

pytestmark = pytest.mark.property


_ZERO_WIDTH_CHARS: Final[frozenset[str]] = frozenset(
    {"\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"}
)

# Codepoints we deliberately exclude from generated text so we never feed
# the post-processor inputs whose normalisation would obscure the property
# we are trying to test. Surrogates ("Cs") are dropped because they cannot
# round-trip through UTF-8; everything below U+20 is a control character
# that the line-ending normaliser would rewrite.
SAFE_TEXT: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        min_codepoint=0x20,
        max_codepoint=0xFFFF,
    ),
    min_size=0,
    max_size=200,
)


def heading_strategy() -> st.SearchStrategy[str]:
    """Generate a single ATX heading line of level 1-6."""
    return st.builds(
        lambda n, text: "#" * n + " " + (text.replace("\n", " ").strip() or "h"),
        st.integers(min_value=1, max_value=6),
        SAFE_TEXT,
    )


def paragraph_strategy() -> st.SearchStrategy[str]:
    """Generate a single paragraph of safe text (possibly multi-line)."""
    return SAFE_TEXT


def link_strategy() -> st.SearchStrategy[str]:
    """Generate an inline Markdown link ``[label](/path)``."""
    return st.builds(
        lambda label, href: f"[{label}](/{href.replace('/', '_')})",
        SAFE_TEXT.filter(lambda s: "]" not in s and "\n" not in s and len(s) > 0),
        SAFE_TEXT.filter(lambda s: ")" not in s and " " not in s and "\n" not in s and len(s) > 0),
    )


# Narrow alphabet so code bodies survive every pre-fence-split normalisation
# step unchanged, enabling property #6 (exact substring preservation).
_CODE_BODY_LINE_ALPHABET: Final[st.SearchStrategy[str]] = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -",
    min_size=0,
    max_size=40,
).map(lambda s: s.rstrip())  # never end a line with whitespace


def _safe_code_body() -> st.SearchStrategy[str]:
    """Generate code-block bodies that survive every pre-fence normalisation step."""
    return (
        st.lists(
            _CODE_BODY_LINE_ALPHABET.filter(lambda s: s != ""),
            min_size=1,
            max_size=6,
        )
        .map(lambda lines: "\n".join(lines).strip())
        .filter(lambda body: "\n\n\n" not in body)
    )


def fenced_code_strategy() -> st.SearchStrategy[str]:
    """Generate a fenced code block with an optional language hint."""
    return st.builds(
        lambda lang, code: f"```{lang}\n{code}\n```",
        st.sampled_from(["", "python", "rust", "bash", "sql", "javascript"]),
        _safe_code_body().filter(lambda s: "```" not in s),
    )


def markdown_doc_strategy() -> st.SearchStrategy[str]:
    """Compose a small Markdown document from heading/paragraph/link/code blocks."""
    block = st.one_of(
        heading_strategy(),
        paragraph_strategy(),
        link_strategy(),
        fenced_code_strategy(),
    )
    return st.lists(block, min_size=0, max_size=10).map(lambda blocks: "\n\n".join(blocks))


# Shared Hypothesis settings. ``HealthCheck.too_slow`` suppressed because
# some properties hit bs4 + the full post-processor and fire intermittently.
PROPERTY_SETTINGS = settings(
    max_examples=200,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)


# Two realistic base URLs (different host, different path depth) so the
# URL rewriter is exercised against multiple resolution contexts.
_BASE_URLS: st.SearchStrategy[str] = st.sampled_from(["https://example.com/x", "https://a.b/y/z"])

# Optional title: either ``None`` or a single-line non-empty string.
_TITLES: st.SearchStrategy[str | None] = st.one_of(
    st.none(),
    SAFE_TEXT.filter(lambda s: bool(s) and "\n" not in s),
)


@PROPERTY_SETTINGS
@given(markdown_doc_strategy(), _BASE_URLS, _TITLES)
def test_postprocess_is_idempotent(doc: str, base_url: str, title: str | None) -> None:
    """Re-running ``postprocess`` on its own output must change nothing."""
    once = postprocess(doc, base_url=base_url, title=title)
    twice = postprocess(once, base_url=base_url, title=title)
    assert once == twice


@PROPERTY_SETTINGS
@given(markdown_doc_strategy(), _BASE_URLS, _TITLES)
def test_postprocess_output_is_nfc(doc: str, base_url: str, title: str | None) -> None:
    """Output is fixed-point under NFC normalisation."""
    out = postprocess(doc, base_url=base_url, title=title)
    assert unicodedata.normalize("NFC", out) == out


@PROPERTY_SETTINGS
@given(markdown_doc_strategy(), _BASE_URLS, _TITLES)
def test_postprocess_strips_zero_width(doc: str, base_url: str, title: str | None) -> None:
    """No zero-width / BOM / WJ codepoint can appear in the output."""
    out = postprocess(doc, base_url=base_url, title=title)
    for ch in _ZERO_WIDTH_CHARS:
        assert ch not in out, f"zero-width U+{ord(ch):04X} leaked into output"


@PROPERTY_SETTINGS
@given(markdown_doc_strategy(), _BASE_URLS, _TITLES)
def test_postprocess_trailing_newline(doc: str, base_url: str, title: str | None) -> None:
    """Output is ``""`` or ends with exactly one ``\\n``."""
    out = postprocess(doc, base_url=base_url, title=title)
    if out == "":
        return
    assert out.endswith("\n"), "non-empty output must end with a newline"
    assert not out.endswith("\n\n"), "output must not end with two or more newlines"


_OUTPUT_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)
_FENCE_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _heading_levels_outside_fences(text: str) -> list[int]:
    """Return ATX heading levels in document order, skipping fenced blocks."""
    levels: list[int] = []
    in_code = False
    fence_char: str | None = None
    fence_len = 0
    for line in text.split("\n"):
        match = _FENCE_LINE_RE.match(line)
        if not in_code:
            if match:
                fence_char = match.group(1)[0]
                fence_len = len(match.group(1))
                in_code = True
            else:
                heading = _OUTPUT_HEADING_RE.match(line)
                if heading:
                    levels.append(len(heading.group(1)))
        else:
            # Inside a fence: only a matching closer ends the segment.
            if match and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_len:
                in_code = False
                fence_char = None
                fence_len = 0
    return levels


@PROPERTY_SETTINGS
@given(markdown_doc_strategy(), _BASE_URLS, _TITLES)
def test_postprocess_heading_monotonicity(doc: str, base_url: str, title: str | None) -> None:
    """Adjacent ATX headings differ in depth by at most +1 level."""
    out = postprocess(doc, base_url=base_url, title=title)
    levels = _heading_levels_outside_fences(out)
    for previous, current in itertools.pairwise(levels):
        assert current <= previous + 1, (
            f"heading skip: level {previous} → {current} in output:\n{out!r}"
        )


_CODE_BODY_RE: Final[re.Pattern[str]] = re.compile(
    r"^```[^\n]*\n(.*?)\n```",
    re.DOTALL | re.MULTILINE,
)


def _extract_code_bodies(text: str) -> list[str]:
    """Return the body of every fenced code block, in document order."""
    return _CODE_BODY_RE.findall(text)


@PROPERTY_SETTINGS
@given(markdown_doc_strategy(), _BASE_URLS, _TITLES)
def test_postprocess_preserves_fenced_code(doc: str, base_url: str, title: str | None) -> None:
    """Each fenced-code body in the input appears verbatim in the output."""
    out = postprocess(doc, base_url=base_url, title=title)
    input_bodies = _extract_code_bodies(doc)
    for body in input_bodies:
        # Empty bodies (e.g. ``\\n\\n```\\n```\\n``) collapse away under
        # the converter's whitespace normalisation; skipping them keeps
        # the property focused on the meaningful preservation guarantee.
        if not body.strip():
            continue
        assert body in out, f"code body lost:\nbody={body!r}\noutput={out!r}"
