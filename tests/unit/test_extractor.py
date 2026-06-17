"""Unit tests for :mod:`pagetomd.extractor`."""

from __future__ import annotations

import dataclasses

import pytest

from pagetomd.exceptions import ExtractionEmptyError
from pagetomd.extractor import ExtractedDoc, extract
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

    assert excinfo.value.context["url"] == "https://example.com/x"
    assert "html_length" in excinfo.value.context


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
