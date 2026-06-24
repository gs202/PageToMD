"""Unit tests for :mod:`pagetomd.extractor`."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from pagetomd.converter import convert
from pagetomd.exceptions import ExtractionEmptyError
from pagetomd.extractor import (
    ExtractedDoc,
    _resolve_title,
    _title_from_html,
    extract,
)
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


def test_title_tag_wins_over_leading_note_admonition() -> None:
    """Regression: the page title must come from ``<title>``, not "Note".

    On SPA documentation portals the body's first heading is often a "Note"
    admonition, and trafilatura's title heuristic latches onto it while
    filing the real title under ``sitename``. The page's own ``<title>`` tag
    carries the correct, author-declared title — with the site name appended
    via a separator — so it must win.
    """
    html = (
        "<html><head><title>Page Title \u2022 Documentation Section "
        "\u2022 Example Docs Portal</title></head>"
        "<body><article><h1>Note</h1>"
        f"<p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    assert result.title == "Page Title"


def test_resolve_title_prefers_title_tag_over_trafilatura() -> None:
    """The page-declared ``<title>`` segment beats trafilatura's guess."""
    assert (
        _resolve_title(
            title_tag="Page Title",
            trafilatura_title="Note",
            site_name="Example Docs Portal",
        )
        == "Page Title"
    )


def test_resolve_title_falls_back_when_title_is_only_site_name() -> None:
    """A ``<title>`` that is just the site name must not shadow a real title.

    Guards the v0.1.0/v0.2.0 SPA-shell case: an unrendered shell whose
    ``<title>`` is nothing but the site name defers to trafilatura's title.
    """
    assert (
        _resolve_title(
            title_tag="Example Docs Portal",
            trafilatura_title="Real Article Heading",
            site_name="Example Docs Portal",
        )
        == "Real Article Heading"
    )


def test_resolve_title_falls_back_when_no_title_tag() -> None:
    """With no ``<title>`` tag, trafilatura's title is used."""
    assert (
        _resolve_title(
            title_tag=None,
            trafilatura_title="Fallback Title",
            site_name=None,
        )
        == "Fallback Title"
    )


def test_title_from_html_strips_site_name_suffix() -> None:
    """The leading segment before a separator is the page-specific title."""
    html = (
        "<html><head><title>Page Title \u2022 Documentation Section "
        "\u2022 Example Docs Portal</title></head>"
        "<body></body></html>"
    )
    assert _title_from_html(html) == "Page Title"


def test_title_from_html_returns_none_without_title_tag() -> None:
    """No ``<title>`` element yields ``None``."""
    assert _title_from_html("<html><body><p>hi</p></body></html>") is None


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


def test_metadata_exception_recovers_title_from_title_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If trafilatura's metadata call blows up, extraction still succeeds.

    The title is recovered from the page's ``<title>`` tag (independent of
    trafilatura's metadata heuristic); every other metadata field is ``None``.
    """

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("metadata gremlin")

    monkeypatch.setattr("pagetomd.extractor.trafilatura.extract_metadata", _boom)

    html = (
        f"<html><head><title>T</title></head>"
        f"<body><article><h1>T</h1><p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    # Title comes from the <title> tag, not trafilatura metadata.
    assert result.title == "T"
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
# Cross-reference link preservation (regression tests for the "see X"
# patterns documented in
# `.idex/plans/2026-06-23-preserve-cross-reference-links.md`).
# ---------------------------------------------------------------------------


def _render_cross_ref_fixture(fixture_html: Callable[[str], str]) -> str:
    """Run the full pipeline on ``cross_reference_links.html`` and return Markdown.

    Centralises the fetch-stub → extract → convert → postprocess wiring so
    each regression test below only has to assert on the rendered output.
    """
    html = fixture_html("cross_reference_links.html")
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
    line: ``"For more information, see [Identity Engine Setup](…)."``
    """
    md = _render_cross_ref_fixture(fixture_html)

    expected = (
        "Identity Engine must be set up. For more information, "
        "see [Identity Engine Setup]"
        "(https://docs.example.com/r/GD6sG6FlxDWxAn13_eZuUQ/"
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

    html = fixture_html("cross_reference_links.html")
    cleaned, _ = _preclean(html, include_comments=False)

    # Structural contract: decorative span unwrapped in the pre-clean tree.
    assert "xreftitle" not in cleaned, (
        f"Decorative ``xreftitle`` span survived pre-clean:\n{cleaned}"
    )

    # End-to-end contract: the Pattern B link still renders.
    md = _render_cross_ref_fixture(fixture_html)
    expected_link = (
        "[Assistant role-based access control]"
        "(https://docs.example.com/r/GD6sG6FlxDWxAn13_eZuUQ/"
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


def test_lift_orphan_anchor_ignores_trigger_word_deep_inside_long_paragraph() -> None:
    """Tail-window cap prevents an early "see" deep in a paragraph from firing the lift.

    Regression guard for a class of false positives where the orphan-anchor
    matcher scanned the entire previous-sibling text. A long paragraph that
    ends in a benign sentence — but happens to contain ``"see"`` earlier
    (e.g. ``"...click here to see..."``) — must NOT trigger the lift on
    an unrelated trailing ``<a>`` sibling.
    """
    long_prefix = "Lorem ipsum click here to see " + ("lorem ipsum " * 50)
    benign_tail = "and then we are done."
    paragraph_text = long_prefix + benign_tail

    snippet = f'<div><p>{paragraph_text}</p><a href="https://example.com/x">Unrelated</a></div>'
    out, lifted = _lift_and_serialize(snippet)

    assert lifted == 0, (
        "Tail-window cap broken: an early 'see' inside a long paragraph fired the "
        "orphan-anchor lift on an unrelated trailing anchor."
    )
    # Anchor must remain an orphan sibling of the paragraph, not pulled inside.
    assert '</p><a href="https://example.com/x">Unrelated</a>' in out


def test_extract_lifts_orphan_anchor_from_post_trafilatura_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second-invocation lift rescues the orphan-anchor shape that emerges in trafilatura output.

    The orphan-anchor pattern (``<a>`` promoted out of an enclosing ``<p>``)
    is most often produced *by* trafilatura's body extraction rather than
    being present in the source HTML. The post-trafilatura ``_lift_orphan_
    anchor_siblings`` invocation at the top of :func:`extract` is the
    load-bearing rescue path; this test patches ``trafilatura.extract`` to
    return a body that already exhibits the orphan-anchor shape and asserts
    that the returned ``cleaned_html`` contains the link inline — proving
    the second invocation lifted it back into the preceding paragraph.
    """
    mangled_body = (
        '<div><p>For more information, see</p><a href="https://example.com/x">Link text</a>.</div>'
    )

    monkeypatch.setattr(
        "pagetomd.extractor.trafilatura.extract",
        lambda *_a, **_k: mangled_body,
    )

    html = (
        f"<html><head><title>T</title></head>"
        f"<body><article><h1>T</h1><p>{_BODY}</p></article></body></html>"
    )
    result = extract(make_fetched_doc(html), make_config())

    # After the lift, the anchor sits inside the preceding ``<p>`` directly
    # following the word ``see`` (separated by whitespace). The exact failure
    # symptom this guards against is an *orphan* ``<a>`` sibling outside the
    # ``<p>`` — i.e. the substring ``</p><a`` appearing in the output.
    assert "see <a " in result.cleaned_html, (
        "Post-trafilatura `_lift_orphan_anchor_siblings` invocation did not "
        "rescue the orphan anchor (link text not inline after 'see').\n"
        f"Got cleaned_html:\n{result.cleaned_html}"
    )
    assert "</p><a" not in result.cleaned_html, (
        "Anchor remained an orphan sibling outside the paragraph.\n"
        f"Got cleaned_html:\n{result.cleaned_html}"
    )


def test_extract_does_not_drop_shared_content_across_pages() -> None:
    """Repeated ``extract`` calls in one process must not drop shared paragraphs.

    Documentation portals repeat the same intro/abstract paragraphs and
    boilerplate prose across many sibling pages.  Trafilatura's
    ``deduplicate`` option keeps a *process-global* LRU cache that persists
    between :func:`extract` calls, so once a paragraph has been seen on a few
    earlier pages it is silently dropped from later pages — making the
    extractor's output depend on how many pages preceded it in a crawl.

    This is the root cause of the crawl-vs-single divergence: a single-page
    fetch (fresh process, empty cache) keeps the paragraph, while the same
    page reached deep in a crawl loses it.  The extractor must produce the
    *same* content for a given page regardless of crawl position.

    The fixture extracts several distinct pages that all share one boilerplate
    paragraph, then asserts the shared paragraph still survives on the final
    page.
    """
    shared = (
        "Understand more about the query language so you can build queries to "
        "gain insight from the data contained in the different data sources "
        "available in the product."
    )

    def page(unique_marker: str) -> str:
        # Each page has unique body text plus the shared boilerplate paragraph.
        body = (
            f"This is the unique main content for page {unique_marker}. It is "
            "long enough that trafilatura's recall heuristics latch onto the "
            "article body and emit it as the main content block."
        )
        return (
            f"<html><head><title>Page {unique_marker}</title></head>"
            f"<body><article><h1>Page {unique_marker}</h1>"
            f"<p>{body}</p><p>{shared}</p></article></body></html>"
        )

    config = make_config()

    # Simulate a crawl: extract several distinct pages in the SAME process so
    # the shared paragraph accumulates in any process-global dedup cache.
    last_result: ExtractedDoc | None = None
    for marker in ("alpha", "bravo", "charlie", "delta", "echo"):
        last_result = extract(make_fetched_doc(page(marker)), config)

    assert last_result is not None
    assert "Understand more about the query language" in last_result.cleaned_html, (
        "Shared boilerplate paragraph was dropped on a later page — the "
        "extractor's output depends on crawl position (cross-page dedup "
        "cache pollution).\n"
        f"Got cleaned_html:\n{last_result.cleaned_html}"
    )


def test_extract_is_deterministic_on_identical_input() -> None:
    """Two ``extract`` calls on byte-identical input must yield identical output.

    Guards against non-deterministic extractor behaviour driven by any
    process-global state (e.g. trafilatura's deduplication LRU cache). The
    same input HTML must always map to the same ``cleaned_html``.
    """
    shared = (
        "Repeated abstract paragraph that documentation portals reuse across "
        "many sibling pages and which must never be dropped on a re-extract."
    )
    html = (
        "<html><head><title>Determinism</title></head>"
        "<body><article><h1>Determinism</h1>"
        f"<p>{_BODY}</p><p>{shared}</p></article></body></html>"
    )
    doc = make_fetched_doc(html)
    config = make_config()

    first = extract(doc, config).cleaned_html
    second = extract(doc, config).cleaned_html

    assert first == second, (
        "extract() produced different output for identical input — "
        "non-deterministic extraction.\n"
        f"first:\n{first}\n\nsecond:\n{second}"
    )
