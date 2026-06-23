"""Unit tests for :mod:`pagetomd.extractor`."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from pagetomd.converter import convert
from pagetomd.exceptions import ExtractionEmptyError
from pagetomd.extractor import ExtractedDoc, extract
from pagetomd.postprocess import postprocess
from tests.conftest import make_config, make_fetched_doc

# A reasonably long article body — trafilatura ignores tiny inputs.
_BODY = (
    "This is meaningful body content that exists to give the extractor "
    "enough material to identify as the main article body. We pad it so "
    "trafilatura's recall heuristics latch onto it."
)


def test_happy_path_returns_title_and_body() -> None:
    html = (
        f"<html><head><title>Title</title></head>"
        f"<body><article><h1>Title</h1><p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    assert isinstance(result, ExtractedDoc)
    assert result.title == "Title"
    assert "meaningful body content" in result.cleaned_html


def test_script_and_style_are_stripped() -> None:
    """``<script>``/``<style>`` payloads must not survive into ``cleaned_html``."""
    html = (
        f"<html><head><title>T</title><style>body{{color:red}}</style></head>"
        f"<body><script>alert(1)</script>"
        f"<article><h1>T</h1><p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    assert "alert(1)" not in result.cleaned_html
    assert "color:red" not in result.cleaned_html


def test_comments_dropped_by_default() -> None:
    """Comments are stripped from the extractor's view of the HTML."""
    html = (
        f"<html><head><title>T</title></head>"
        f"<body><article><h1>T</h1><!-- tracking pixel -->"
        f"<p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    assert "tracking pixel" not in result.cleaned_html


def test_comments_kept_when_requested() -> None:
    """``include_comments=True`` keeps comment nodes during the pre-clean."""
    html = (
        f"<html><head><title>T</title></head>"
        f"<body><article><h1>T</h1>"
        f"<!-- keep-this-comment -->"
        f"<p>{_BODY}</p></article></body></html>"
    )
    # Verify at the pre-clean stage directly.
    from pagetomd.extractor import _preclean

    cleaned, _ = _preclean(html, include_comments=True)
    assert "keep-this-comment" in cleaned

    cleaned_dropped, _ = _preclean(html, include_comments=False)
    assert "keep-this-comment" not in cleaned_dropped


def test_junk_patterns_remove_cookie_and_newsletter() -> None:
    """Cookie banners and newsletter widgets get the axe by class/id."""
    from pagetomd.extractor import _preclean

    html = (
        '<html><body><div class="cookie-banner">JUNK1</div>'
        '<div id="newsletter-signup">JUNK2</div>'
        f"<article><h1>T</h1><p>{_BODY}</p></article></body></html>"
    )
    cleaned, removed = _preclean(html, include_comments=False)
    assert "JUNK1" not in cleaned
    assert "JUNK2" not in cleaned
    assert removed["junk_pattern"] >= 2


def test_role_navigation_removed() -> None:
    """Elements with ``role="navigation"`` (and friends) are dropped."""
    from pagetomd.extractor import _preclean

    html = (
        '<html><body><div role="navigation">NAV</div>'
        '<div role="search">SEARCHBOX</div>'
        f"<article><h1>T</h1><p>{_BODY}</p></article></body></html>"
    )
    cleaned, removed = _preclean(html, include_comments=False)
    assert "NAV" not in cleaned
    assert "SEARCHBOX" not in cleaned
    assert removed["role"] >= 2


def test_anchor_attrs_scrubbed_keep_href_only() -> None:
    """Pre-clean strips ``target``/``rel``/``data-*`` but keeps ``href``."""
    from pagetomd.extractor import _preclean

    html = (
        "<html><body><article><h1>T</h1>"
        '<p>see <a href="/x" target="_blank" rel="noopener" '
        'data-tracking="y">Link</a> here</p></article></body></html>'
    )
    cleaned, removed = _preclean(html, include_comments=False)
    assert 'href="/x"' in cleaned
    assert "target=" not in cleaned
    assert "rel=" not in cleaned
    assert "data-tracking" not in cleaned
    assert removed["link_attrs"] >= 1


def test_empty_body_raises_extraction_empty_error() -> None:
    """An empty body produces no extractable content → typed error."""
    with pytest.raises(ExtractionEmptyError) as excinfo:
        extract(make_fetched_doc("<html><body></body></html>"), make_config())

    assert "no readable content" in excinfo.value.message.lower()


@pytest.mark.parametrize(
    "return_value",
    [None, "   \n  "],
    ids=["none", "whitespace"],
)
def test_extraction_empty_when_trafilatura_returns_falsy(
    monkeypatch: pytest.MonkeyPatch, return_value: str | None
) -> None:
    """None and whitespace-only trafilatura output both raise ExtractionEmptyError."""
    monkeypatch.setattr("pagetomd.extractor.trafilatura.extract", lambda *a, **k: return_value)
    with pytest.raises(ExtractionEmptyError):
        extract(make_fetched_doc(f"<html><body><p>{_BODY}</p></body></html>"), make_config())


def test_metadata_exception_falls_back_to_all_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """If trafilatura's metadata call blows up, extraction still succeeds."""

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("metadata gremlin")

    monkeypatch.setattr("pagetomd.extractor.trafilatura.extract_metadata", _boom)

    html = (
        f"<html><head><title>T</title></head>"
        f"<body><article><h1>T</h1><p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    assert result.title is None
    assert result.author is None
    assert result.date is None
    assert result.description is None
    assert result.site_name is None
    assert result.language is None
    assert "meaningful body content" in result.cleaned_html


def test_extracted_doc_is_frozen_and_slotted() -> None:
    """Instances are immutable; mutation attempts raise ``FrozenInstanceError``."""
    inst = ExtractedDoc(
        title="t",
        author=None,
        date=None,
        description=None,
        site_name=None,
        language=None,
        cleaned_html="<p>x</p>",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        inst.title = "nope"  # type: ignore[misc]


def test_structural_wrapper_dropped_with_article_sibling() -> None:
    """A ``<nav>``/``<header>`` sibling of ``<article>`` is removed."""
    from pagetomd.extractor import _preclean

    html = (
        "<html><body>"
        "<header>SITE HEADER</header>"
        "<nav>NAV LINKS</nav>"
        "<article><h1>Real</h1><p>Body</p></article>"
        "<footer>FOOTER</footer>"
        "<aside>ASIDE</aside>"
        "</body></html>"
    )
    cleaned, removed = _preclean(html, include_comments=False)
    assert "SITE HEADER" not in cleaned
    assert "NAV LINKS" not in cleaned
    assert "FOOTER" not in cleaned
    assert "ASIDE" not in cleaned
    assert "Real" in cleaned
    assert removed["structural"] >= 4


def test_structural_wrapper_kept_when_only_child() -> None:
    """A ``<header>`` that wraps the whole article is kept (no siblings)."""
    from pagetomd.extractor import _preclean

    html = "<html><body><header><h1>T</h1><p>Body</p></header></body></html>"
    cleaned, removed = _preclean(html, include_comments=False)
    # ``<header>`` should still be present because it has no article-like
    # sibling — dropping it would orphan everything inside.
    assert "<header>" in cleaned
    assert removed["structural"] == 0


def test_title_falls_back_to_title_tag_without_h1() -> None:
    """When the article has no ``<h1>``, the page ``<title>`` still wins."""
    extra = (
        "And here is a second paragraph to make trafilatura comfortable "
        "that this is article content rather than chrome or boilerplate."
    )
    third = "Yet another paragraph reinforcing the content density signal."
    html = (
        f"<html><head><title>From Title Tag</title></head>"
        f"<body><article><p>{_BODY}</p><p>{extra}</p><p>{third}</p>"
        f"</article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    assert result.title == "From Title Tag"


def test_preclean_handles_nested_junk_siblings_without_crashing() -> None:
    """Stacked junk-class subtrees coexist without ``AttributeError``.

    Reproducer for a failure where
    ``find_all(True)`` handed back stale ``Tag`` references after a
    sibling ``decompose()`` and the loop tripped on ``tag.attrs`` /
    ``tag.name``.
    """
    html = (
        "<html><body>"
        "<article><h1>OK</h1><p>Body</p></article>"
        '<aside class="newsletter"><div class="signup">junk</div></aside>'
        '<section class="comments">'
        '<div class="comment">A</div>'
        '<div class="comment">B</div>'
        "</section>"
        '<div class="share-buttons">'
        '<a class="share-twitter">T</a>'
        '<a class="share-facebook">F</a>'
        "</div>"
        "</body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())
    assert "Body" in result.cleaned_html
    # And the junk really is gone.
    assert "newsletter" not in result.cleaned_html
    assert "share-twitter" not in result.cleaned_html


@pytest.mark.parametrize(
    ("html_snippet", "expected_langs", "expected_count"),
    [
        (
            '<pre><code class="language-python">x = 1</code></pre>',
            ['data-lang="python"'],
            1,
        ),
        (
            '<pre><code class="lang-rust">fn main() {}</code></pre>'
            '<pre><code class="highlight-go">package main</code></pre>',
            ['data-lang="rust"', 'data-lang="go"'],
            2,
        ),
        (
            "<pre><code>just text</code></pre>",
            [],
            0,
        ),
    ],
    ids=["language_prefix", "lang_and_highlight_prefix", "no_lang"],
)
def test_preclean_lang_annotation(
    html_snippet: str, expected_langs: list[str], expected_count: int
) -> None:
    from pagetomd.extractor import _preclean

    html = f"<html><body>{html_snippet}</body></html>"
    cleaned, removed = _preclean(html, include_comments=True)
    for lang in expected_langs:
        assert lang in cleaned
    assert removed["lang_annotated"] == expected_count


def test_preclean_embeds_text_sentinel_for_lang() -> None:
    """A text-sentinel marker survives any downstream attribute stripper."""
    from pagetomd.extractor import LANG_SENTINEL_PREFIX, _preclean

    html = '<html><body><pre><code class="language-python">x = 1</code></pre></body></html>'
    cleaned, _ = _preclean(html, include_comments=True)
    assert f"{LANG_SENTINEL_PREFIX}python" in cleaned


_PAD = " More padding so trafilatura keeps the article. " * 6


def test_extract_captures_base_href_from_head() -> None:
    """The extractor surfaces ``<base href>`` so the pipeline can use it."""
    html = (
        '<html><head><title>T</title><base href="https://cdn.example/">'
        f"</head><body><article><h1>T</h1><p>{_BODY}{_PAD}</p>"
        f"<p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())
    assert result.base_href == "https://cdn.example/"


def test_extract_returns_none_when_base_href_missing() -> None:
    """No ``<base>`` tag → ``base_href`` is ``None``."""
    html = (
        f"<html><head><title>T</title></head>"
        f"<body><article><h1>T</h1><p>{_BODY}{_PAD}</p>"
        f"<p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())
    assert result.base_href is None


def test_extract_ignores_empty_base_href() -> None:
    """An empty / whitespace-only ``href`` is treated as absent."""
    html = (
        '<html><head><title>T</title><base href="   ">'
        f"</head><body><article><h1>T</h1><p>{_BODY}{_PAD}</p>"
        f"<p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())
    assert result.base_href is None


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<html><head><base href="/x/"></head><body>y</body></html>', "/x/"),
        ("<html><head></head><body>y</body></html>", None),
        ('<html><head><base href="  "></head><body>y</body></html>', None),
    ],
    ids=["present", "absent", "whitespace_only"],
)
def test_extract_base_href_helper(html: str, expected: str | None) -> None:
    from pagetomd.extractor import _extract_base_href

    assert _extract_base_href(html) == expected


# ---------------------------------------------------------------------------
# Preclean-fallback path (line 159)
# ---------------------------------------------------------------------------


def test_preclean_fallback_rescues_content_in_junk_named_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When preclean over-fires and removes the main content, the fallback
    minimal-strip pass should still recover it.

    We simulate the over-fire by making trafilatura return ``None`` on the
    first call (post-preclean) but return real content on the second call
    (post-minimal-strip). This exercises the ``soup_fallback`` branch at
    lines 157-160.
    """
    call_count = 0

    def _patched_extract(html: str, **kwargs: object) -> str | None:
        nonlocal call_count
        call_count += 1
        # First call (after preclean) → simulate over-fire.
        if call_count == 1:
            return None
        # Second call (fallback) → return content regardless of HTML.
        return f"<p>{_BODY}</p>"

    monkeypatch.setattr("pagetomd.extractor.trafilatura.extract", _patched_extract)

    html = (
        f"<html><head><title>T</title></head>"
        f"<body><article><h1>T</h1><p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    assert call_count == 2
    assert "meaningful body content" in result.cleaned_html


# ---------------------------------------------------------------------------
# _extract_uuid_sections (lines 232-249)
# ---------------------------------------------------------------------------


def test_extract_uuid_sections_returns_none_when_no_uuid_sections() -> None:
    """When no ``<section id="UUID-…">`` elements exist, the helper returns None."""
    from pagetomd.extractor import _extract_uuid_sections

    html = "<html><body><section id='regular-section'><p>hi</p></section></body></html>"
    result = _extract_uuid_sections(html, {}, object())
    assert result is None


def test_extract_uuid_sections_concatenates_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UUID sections are each extracted individually and concatenated."""
    call_num = 0

    def _fake_extract(html: str, **kwargs: object) -> str | None:
        nonlocal call_num
        call_num += 1
        # Return distinct content per call so we can verify concatenation.
        return f"<p>section-{call_num}</p>"

    monkeypatch.setattr("pagetomd.extractor.trafilatura.extract", _fake_extract)

    import structlog

    bound = structlog.get_logger()
    from pagetomd.extractor import _extract_uuid_sections

    html = (
        "<html><body>"
        "<section id='UUID-aaa'><p>Topic A</p></section>"
        "<section id='UUID-bbb'><p>Topic B</p></section>"
        "</body></html>"
    )
    result = _extract_uuid_sections(html, {}, bound)

    assert result is not None
    assert "section-1" in result
    assert "section-2" in result
    assert call_num == 2


def test_extract_uuid_sections_returns_none_when_all_extractions_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns ``None`` when trafilatura returns ``None`` for every UUID section."""
    monkeypatch.setattr("pagetomd.extractor.trafilatura.extract", lambda *a, **k: None)

    import structlog

    bound = structlog.get_logger()
    from pagetomd.extractor import _extract_uuid_sections

    html = "<html><body><section id='UUID-aaa'><p>x</p></section></body></html>"
    result = _extract_uuid_sections(html, {}, bound)
    assert result is None


def test_extract_full_pipeline_falls_through_to_uuid_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both preclean and fallback passes yield nothing, UUID sections
    are attempted and their content is returned."""
    call_count = 0

    def _fake_extract(html: str, **kwargs: object) -> str | None:
        nonlocal call_count
        call_count += 1
        # First two calls (preclean + fallback) → None.
        if call_count <= 2:
            return None
        # UUID-section calls → real content.
        return f"<p>{_BODY}</p>"

    monkeypatch.setattr("pagetomd.extractor.trafilatura.extract", _fake_extract)

    html = f"<html><body><section id='UUID-topic-1'><p>{_BODY}</p></section></body></html>"
    result = extract(make_fetched_doc(html), make_config())

    assert "meaningful body content" in result.cleaned_html
    assert call_count >= 3


# ---------------------------------------------------------------------------
# Branch misses — structural loop detached-parent guard (line 320)
# ---------------------------------------------------------------------------


def test_preclean_structural_detached_parent_guard() -> None:
    """Nested structural tags sharing a parent don't crash when the outer one
    is decomposed first, leaving the inner with ``parent is None``."""
    from pagetomd.extractor import _preclean

    # A ``<nav>`` nested inside a ``<footer>`` — both are structural. When
    # ``<footer>`` is decomposed first the inner ``<nav>`` becomes detached.
    # The guard at line 316 (``if tag.parent is None: continue``) prevents a
    # crash on the orphaned inner tag.
    html = (
        "<html><body>"
        "<article><h1>Real</h1><p>Body</p></article>"
        "<footer><nav>NAV INSIDE FOOTER</nav></footer>"
        "</body></html>"
    )
    # Should not raise; structural items are removed.
    cleaned, removed = _preclean(html, include_comments=False)
    assert "NAV INSIDE FOOTER" not in cleaned
    assert removed["structural"] >= 1


# ---------------------------------------------------------------------------
# Branch miss — role value not in _DROP_ROLES (line 349 → 341 arc)
# ---------------------------------------------------------------------------


def test_preclean_non_junk_role_is_kept() -> None:
    """Elements with an irrelevant role (e.g. ``"main"``) are NOT dropped."""
    from pagetomd.extractor import _preclean

    html = '<html><body><div role="main"><p>Keep me</p></div></body></html>'
    cleaned, removed = _preclean(html, include_comments=False)
    assert "Keep me" in cleaned
    assert removed["role"] == 0


# ---------------------------------------------------------------------------
# Branch miss — match_lang_class with a plain string class attribute (line 398)
# ---------------------------------------------------------------------------


def test_match_lang_class_handles_string_class_attribute() -> None:
    """``match_lang_class`` copes when bs4 gives a plain str for ``class``."""
    from bs4 import BeautifulSoup

    from pagetomd.extractor import match_lang_class

    # Force a Tag with a plain-string class value by monkeypatching ``get``.
    soup = BeautifulSoup("<code class='language-ruby'>x</code>", "lxml")
    tag = soup.find("code")
    assert tag is not None

    # Temporarily replace get() to return a bare string instead of a list.
    original_get = tag.get

    def _str_get(key: str, default: object = None) -> object:
        if key == "class":
            return "language-ruby"
        return original_get(key, default)

    tag.get = _str_get  # type: ignore[method-assign]

    result = match_lang_class(tag)  # type: ignore[arg-type]
    assert result == "ruby"


# ---------------------------------------------------------------------------
# PANW cross-reference link preservation (regression tests for the
# FluidTopics/Paligo "see X" patterns documented in
# `.idex/plans/2026-06-23-preserve-panw-cross-reference-links.md`).
# ---------------------------------------------------------------------------


def _render_panw_fixture(fixture_html: Callable[[str], str]) -> str:
    """Run the full pipeline on ``panw_cross_refs.html`` and return Markdown.

    Centralises the fetch-stub → extract → convert → postprocess wiring so
    each regression test below only has to assert on the rendered output.
    """
    html = fixture_html("panw_cross_refs.html")
    doc = make_fetched_doc(html, url="https://example.com/x")
    cfg = make_config()
    extracted = extract(doc, cfg)
    body = convert(extracted.cleaned_html, cfg)
    return postprocess(body, base_url="https://example.com/x")


def test_preclean_lifts_orphan_anchor_into_preceding_paragraph(
    fixture_html: Callable[[str], str],
) -> None:
    """Pattern A: bare ``<a>`` inside ``<li><p>…see X</p></li>`` survives intact.

    Trafilatura currently promotes the ``<a>`` out of the inner ``<p>``,
    producing an orphan-anchor sibling that renders as two visually-detached
    Markdown blocks (a sentence ending in ``see`` followed by a dangling
    link on its own line). The fix lifts the anchor back into the sentence
    so the rendered Markdown keeps the entire phrase on a single bullet
    line: ``"For more information, see [Cloud Identity Engine](…)."``
    """
    md = _render_panw_fixture(fixture_html)

    expected = (
        "Cloud Identity Engine must be set up. For more information, "
        "see [Cloud Identity Engine]"
        "(https://docs-cortex.paloaltonetworks.com/r/GD6sG6FlxDWxAn13_eZuUQ/"
        "c~Ez47XfCHk0H2jLU85Vgg)."
    )
    assert expected in md, (
        f"Pattern A link did not survive intact on its bullet line.\n"
        f"Looked for: {expected!r}\nRendered Markdown was:\n{md}"
    )


def test_preclean_unwraps_xreftitle_span_inside_anchor(
    fixture_html: Callable[[str], str],
) -> None:
    """Pattern B: ``<a><span class="xreftitle">…</span></a>`` is normalised.

    The decorative ``<span class="xreftitle">`` must be unwrapped *during
    pre-clean* so the anchor's link text becomes a direct text child of
    the ``<a>``. End-to-end Markdown today already happens to render this
    correctly because the converter strips inner span attributes, so the
    real contract this test pins down is the structural unwrap inside
    ``_preclean``: the rendered Markdown keeps the link, and the cleaned
    HTML fed to trafilatura contains no ``xreftitle`` markup at all.
    """
    from pagetomd.extractor import _preclean

    html = fixture_html("panw_cross_refs.html")
    cleaned, _ = _preclean(html, include_comments=False)

    # Structural contract: decorative span unwrapped in the pre-clean tree.
    assert "xreftitle" not in cleaned, (
        f"Decorative ``xreftitle`` span survived pre-clean:\n{cleaned}"
    )

    # End-to-end contract: the Pattern B link still renders.
    md = _render_panw_fixture(fixture_html)
    expected_link = (
        "[Agentic Assistant role-based access control]"
        "(https://docs-cortex.paloaltonetworks.com/r/GD6sG6FlxDWxAn13_eZuUQ/"
        "lC97_80YTaLhcwWrxkWjoA)"
    )
    assert expected_link in md, (
        f"Pattern B link missing from rendered Markdown.\n"
        f"Looked for: {expected_link!r}\nRendered Markdown was:\n{md}"
    )
    assert "xreftitle" not in md, (
        f"Decorative ``xreftitle`` class leaked into Markdown output:\n{md}"
    )


@pytest.mark.parametrize(
    ("html_snippet", "should_unwrap", "case_id"),
    [
        # Bare decorative span — unwrap.
        (
            '<a href="/x"><span class="xreftitle">Linked title</span></a>',
            True,
            "decorative_xreftitle",
        ),
        # No class at all — still a single child, still decorative.
        ('<a href="/x"><span>Linked title</span></a>', True, "bare_span"),
        # Multiple children — leave the span alone (an icon span next to text).
        (
            '<a href="/x"><span class="icon">★</span> Linked title</a>',
            False,
            "multiple_children",
        ),
        # Span carries an explicit role — keep, it is semantically meaningful.
        (
            '<a href="/x"><span role="img" class="badge">Linked title</span></a>',
            False,
            "role_attribute",
        ),
        # Span carries an aria-* hint — keep.
        (
            '<a href="/x"><span aria-label="external link">Linked title</span></a>',
            False,
            "aria_label",
        ),
        # Span class is on the accessibility blocklist — keep.
        (
            '<a href="/x"><span class="sr-only">Linked title</span></a>',
            False,
            "sr_only_blocklist",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_unwrap_decorative_anchor_spans_edge_cases(
    html_snippet: str, should_unwrap: bool, case_id: str
) -> None:
    """Pin down the conservative contract of the decorative-span unwrap."""
    from pagetomd.extractor import _preclean

    html = f"<html><body><article>{html_snippet}</article></body></html>"
    cleaned, removed = _preclean(html, include_comments=False)

    if should_unwrap:
        assert "<span" not in cleaned, (
            f"[{case_id}] expected <span> to be unwrapped, got:\n{cleaned}"
        )
        assert removed["decorative_spans"] == 1
    else:
        assert "<span" in cleaned, f"[{case_id}] expected <span> to be preserved, got:\n{cleaned}"
        assert removed["decorative_spans"] == 0


# ---------------------------------------------------------------------------
# `_lift_orphan_anchor_siblings` helper — exhaustive edge-case coverage.
# ---------------------------------------------------------------------------


def _lift_and_serialize(snippet: str) -> tuple[str, int]:
    """Run ``_lift_orphan_anchor_siblings`` over ``snippet`` and return (html, lifted)."""
    from bs4 import BeautifulSoup

    from pagetomd.extractor import _lift_orphan_anchor_siblings

    soup = BeautifulSoup(snippet, "lxml")
    lifted = _lift_orphan_anchor_siblings(soup)
    body = soup.body
    return ("".join(str(c) for c in body.contents) if body is not None else str(soup), lifted)


def test_lift_orphan_anchor_basic_p_sibling() -> None:
    """Anchor whose previous sibling is a ``<p>`` ending in 'see' is lifted."""
    snippet = (
        '<div><p>For more information, see</p><a href="https://example.com/x">Link text</a>.</div>'
    )
    out, lifted = _lift_and_serialize(snippet)
    assert lifted == 1
    assert '<p>For more information, see <a href="https://example.com/x">Link text</a>.</p>' in out


def test_lift_orphan_anchor_basic_li_sibling() -> None:
    """Same lift applies when the trigger sentence lives in a ``<li>``."""
    snippet = (
        '<ul><li>For more information, see</li><a href="https://example.com/x">Link text</a>.</ul>'
    )
    out, lifted = _lift_and_serialize(snippet)
    assert lifted == 1
    assert (
        '<li>For more information, see <a href="https://example.com/x">Link text</a>.</li>' in out
    )


def test_lift_orphan_anchor_skips_non_trigger_sentence() -> None:
    """No trigger phrase → anchor is left alone (false-positive guard)."""
    snippet = (
        "<div>"
        "<p>Unrelated sentence that does not end with the trigger phrase.</p>"
        '<a href="https://example.com/x">Standalone link</a>'
        "</div>"
    )
    out, lifted = _lift_and_serialize(snippet)
    assert lifted == 0
    assert '</p><a href="https://example.com/x">Standalone link</a>' in out


def test_lift_orphan_anchor_skips_when_no_p_or_li_sibling() -> None:
    """Previous sibling is a <div> → not a target container; leave alone."""
    snippet = (
        "<section>"
        "<div>For more information, see</div>"
        '<a href="https://example.com/x">Link</a>.'
        "</section>"
    )
    out, lifted = _lift_and_serialize(snippet)
    assert lifted == 0
    assert "<div>For more information, see</div>" in out


def test_lift_orphan_anchor_is_idempotent() -> None:
    """A second pass on already-lifted HTML must be a no-op."""
    from bs4 import BeautifulSoup

    from pagetomd.extractor import _lift_orphan_anchor_siblings

    snippet = '<div><p>For more information, see</p><a href="https://example.com/x">Link</a>.</div>'
    soup = BeautifulSoup(snippet, "lxml")
    first = _lift_orphan_anchor_siblings(soup)
    second = _lift_orphan_anchor_siblings(soup)
    assert first == 1
    assert second == 0


@pytest.mark.parametrize(
    "trailing",
    [".", ",", ";", ":"],
    ids=["period", "comma", "semicolon", "colon"],
)
def test_lift_orphan_anchor_pulls_trailing_punctuation(trailing: str) -> None:
    """Trailing punctuation NavigableString travels back into the paragraph."""
    snippet = (
        "<div>"
        "<p>For more information, see</p>"
        f'<a href="https://example.com/x">Link</a>{trailing}'
        "</div>"
    )
    out, lifted = _lift_and_serialize(snippet)
    assert lifted == 1
    assert f'<a href="https://example.com/x">Link</a>{trailing}</p>' in out
