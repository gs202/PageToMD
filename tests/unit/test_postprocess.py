"""Unit tests for :mod:`pagetomd.postprocess`.

Pure string-in / string-out assertions; idempotence is also covered in
the ``tests/property/`` suite.
"""

from __future__ import annotations

import pytest

from pagetomd import postprocess as postprocess_mod
from pagetomd.exceptions import ConversionError
from pagetomd.postprocess import postprocess

BASE_URL = "https://example.com/dir/page"


def test_nfc_normalisation_combines_decomposed_accents() -> None:
    """Combining acute (e + U+0301) collapses to precomposed ``é``."""
    decomposed = "e\u0301"
    out = postprocess(decomposed, base_url=BASE_URL)
    assert out == "\u00e9\n"


def test_zero_width_and_bom_characters_stripped() -> None:
    """All five zero-width / BOM codepoints are removed in one pass."""
    raw = "a\u200bb\u200c\u200d\ufeff\u2060c"
    out = postprocess(raw, base_url=BASE_URL)
    assert out == "abc\n"


def test_crlf_and_cr_line_endings_normalised_to_lf() -> None:
    """Mixed ``\\r\\n`` and bare ``\\r`` collapse to ``\\n``."""
    out = postprocess("a\r\nb\rc", base_url=BASE_URL)
    assert out == "a\nb\nc\n"


def test_trailing_whitespace_stripped_per_line() -> None:
    """Trailing spaces and tabs on every line are removed."""
    out = postprocess("foo   \nbar\t\n", base_url=BASE_URL)
    assert out == "foo\nbar\n"


def test_blank_line_runs_collapsed_to_two() -> None:
    """3+ consecutive newlines collapse to exactly two (one blank line)."""
    out = postprocess("a\n\n\n\n\nb", base_url=BASE_URL)
    assert out == "a\n\nb\n"


def test_trailing_newline_is_exactly_one() -> None:
    """Missing trailing newline is added; multiple trailing newlines collapse."""
    assert postprocess("foo", base_url=BASE_URL) == "foo\n"
    assert postprocess("foo\n\n\n", base_url=BASE_URL) == "foo\n"


def test_zero_h1_with_title_prepends_atx_h1() -> None:
    """A non-empty ``title`` becomes a leading ``# title`` when no H1 exists."""
    out = postprocess("Hello world", base_url=BASE_URL, title="My Page")
    assert out.startswith("# My Page\n\n")
    assert "Hello world" in out


def test_duplicate_h1s_demoted_to_h2() -> None:
    """When multiple H1s are present, the first stays and the rest become H2."""
    out = postprocess("# A\n# B\n# C", base_url=BASE_URL)
    assert out == "# A\n\n## B\n\n## C\n"


def test_skipped_heading_level_promoted_one_level_at_a_time() -> None:
    """H1 → H3 collapses to H1 → H2; H1 → H4 also collapses to H1 → H2."""
    assert postprocess("# A\n### C", base_url=BASE_URL) == "# A\n\n## C\n"
    assert postprocess("# A\n#### D", base_url=BASE_URL) == "# A\n\n## D\n"


def test_title_prepend_demotes_orphan_deep_first_heading() -> None:
    """Title prepend + orphan ``### h`` collapses to ``# Title\\n\\n## h``.

    Regression: without pre-seeding ``last_level=1`` to mirror the
    synthetic H1 the title prepend will introduce, the first real heading
    kept its raw depth and produced ``# Title\\n\\n### h`` — a
    skipped-level violation that *also* broke idempotence (the second pass
    saw the now-present H1, applied the no-skip rule, and emitted ``## h``).
    """
    out = postprocess("### h", base_url=BASE_URL, title="My Page")
    assert out == "# My Page\n\n## h\n"


@pytest.mark.parametrize("blank_title", ["", " ", "   ", "\t"])
def test_whitespace_only_title_treated_as_missing(blank_title: str) -> None:
    """A title that strips to ``""`` is not prepended.

    Regression: a title of ``" "`` was being prepended as ``# ``
    (heading with empty text). The trailing-whitespace strip then
    collapsed the line to ``#``, which fails ATX matching on the next
    pass — so the next pass saw zero H1s again and prepended a second
    synthetic heading, breaking idempotence. Treating blank titles as
    missing keeps the contract simple: a non-empty stripped title or
    no title at all.
    """
    out = postprocess("body text", base_url=BASE_URL, title=blank_title)
    assert out == "body text\n"
    assert not out.startswith("#")


def test_title_is_stripped_before_prepend() -> None:
    """Leading / trailing whitespace in ``title`` is trimmed before prepending."""
    out = postprocess("body", base_url=BASE_URL, title="  Hello  ")
    assert out == "# Hello\n\nbody\n"


def test_setext_headings_converted_to_atx() -> None:
    """``=====`` becomes H1, ``-----`` becomes H2 (canonical normalisation)."""
    src = "Title\n=====\n\nSub\n-----"
    assert postprocess(src, base_url=BASE_URL) == "# Title\n\n## Sub\n"


def test_headings_inside_fenced_code_block_untouched() -> None:
    """A fenced block containing ``### in code`` survives verbatim."""
    src = "# Outer\n\n```\n### in code\n```\n"
    out = postprocess(src, base_url=BASE_URL)
    assert "### in code" in out
    # The outer H1 stays an H1 — fenced contents are invisible to the
    # heading walker, so the fenced ``### in code`` does not trigger
    # the no-skip promotion rule.
    assert out.startswith("# Outer")


def test_root_relative_link_becomes_absolute() -> None:
    """``/a/b`` resolves against the base URL's scheme + host."""
    out = postprocess("[x](/a/b)", base_url=BASE_URL)
    assert out == "[x](https://example.com/a/b)\n"


def test_relative_path_link_resolved_against_base_directory() -> None:
    """``other.html`` resolves against the base URL's directory."""
    out = postprocess("[x](other.html)", base_url=BASE_URL)
    assert out == "[x](https://example.com/dir/other.html)\n"


def test_image_relative_src_rewritten() -> None:
    """``![alt](img.png)`` is rewritten the same way as a link."""
    out = postprocess("![alt](img.png)", base_url=BASE_URL)
    assert out == "![alt](https://example.com/dir/img.png)\n"


def test_fragment_only_link_left_untouched() -> None:
    """``[x](#section)`` is treated as an in-page anchor and not rewritten."""
    out = postprocess("[x](#section)", base_url=BASE_URL)
    assert out == "[x](#section)\n"


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert1",
        "JavaScript:alert1",
        "vbscript:msgbox1",
        "VBScript:msgbox1",
        "data:text/html;foo",
    ],
)
def test_dangerous_scheme_link_neutralised(href: str) -> None:
    """Script-bearing link targets collapse to an inert ``#`` anchor.

    The link text is preserved; only the unsafe target is replaced so no
    ``javascript:`` / ``vbscript:`` / ``data:text/html`` payload can reach
    the rendered output. Casing must not bypass the check.
    """
    out = postprocess(f"[click]({href})", base_url=BASE_URL)
    assert out == "[click](#)\n"
    assert "javascript:" not in out.lower()
    assert "vbscript:" not in out.lower()


def test_dangerous_scheme_neutralised_via_absolutise_directly() -> None:
    """Exercises ``_absolutise`` directly for hrefs the link regex can't parse."""
    from pagetomd.postprocess import _absolutise

    for href in (
        "javascript:alert(1)",
        "  JavaScript:alert(1)",
        "vbscript:msgbox(1)",
        "data:text/html,<script>alert(1)</script>",
    ):
        assert _absolutise(href, BASE_URL) == "#"


def test_dangerous_scheme_in_reference_definition_neutralised() -> None:
    """A ``javascript:`` reference-style definition is neutralised too."""
    out = postprocess("[id]: javascript:alert(1)", base_url=BASE_URL)
    assert "javascript:" not in out.lower()
    assert "[id]: #" in out


@pytest.mark.parametrize(
    "url",
    [
        "mailto:a@example.com",
        "tel:+15551234567",
        "data:text/plain;base64,SGVsbG8=",
        "http://other.example/",
        "https://other.example/",
        "ftp://files.example.com/file.txt",
    ],
)
def test_already_absolute_url_schemes_skipped(url: str) -> None:
    """Any URL with an absolute scheme is passed through verbatim."""
    src = f"[x]({url})"
    out = postprocess(src, base_url=BASE_URL)
    assert out == f"[x]({url})\n"


def test_reference_style_link_definition_rewritten_with_title_preserved() -> None:
    """``[id]: /a 'title'`` becomes ``[id]: <absolute> 'title'``."""
    out = postprocess("[id]: /a 'title'", base_url=BASE_URL)
    assert out == "[id]: https://example.com/a 'title'\n"


def test_url_inside_fenced_code_block_not_rewritten() -> None:
    """Relative URLs inside a fenced block stay relative."""
    src = "```\nsee [x](/a/b)\n```\n"
    out = postprocess(src, base_url=BASE_URL)
    assert "[x](/a/b)" in out
    assert "https://example.com/a/b" not in out


@pytest.mark.parametrize(
    "src,kwargs",
    [
        ("e\u0301", {"base_url": BASE_URL}),
        ("# A\n# B\n# C", {"base_url": BASE_URL}),
        ("# A\n### C", {"base_url": BASE_URL}),
        ("[x](/a/b)", {"base_url": BASE_URL}),
        # Title + zero-H1 path is especially fragile — verify the second
        # pass does NOT prepend a second ``# My Page``.
        ("Body text", {"base_url": BASE_URL, "title": "My Page"}),
    ],
)
def test_postprocess_is_idempotent(src: str, kwargs: dict[str, object]) -> None:
    """Running ``postprocess`` twice yields the same string as running it once."""
    once = postprocess(src, **kwargs)  # type: ignore[arg-type]
    twice = postprocess(once, **kwargs)  # type: ignore[arg-type]
    assert once == twice


def test_title_ignored_when_h1_already_present() -> None:
    """An explicit ``title`` is silently ignored if the body already has an H1."""
    out = postprocess("# Hello", base_url=BASE_URL, title="X")
    # No second ``# X`` should appear — count occurrences explicitly.
    assert out.count("# Hello") == 1
    assert "# X" not in out


def test_no_title_and_zero_h1_leaves_headings_alone() -> None:
    """Without a title, a body with no H1 is not given a synthetic one."""
    out = postprocess("Just a paragraph.", base_url=BASE_URL)
    assert out == "Just a paragraph.\n"
    assert not out.startswith("#")


def test_empty_input_returns_empty_string() -> None:
    """Locked behaviour: empty in → empty out (no synthetic trailing newline)."""
    assert postprocess("", base_url=BASE_URL) == ""
    assert postprocess("", base_url=BASE_URL, title="Anything") == ""


def test_internal_helper_failure_wrapped_in_conversion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception from a helper surfaces as ``ConversionError``."""

    def _boom(*_args: object, **_kwargs: object) -> list[tuple[bool, str]]:
        raise RuntimeError("simulated parser explosion")

    monkeypatch.setattr(postprocess_mod, "_split_fenced_blocks", _boom)

    with pytest.raises(ConversionError) as excinfo:
        postprocess("any content", base_url=BASE_URL)

    assert "post-processing failed" in excinfo.value.message.lower()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
