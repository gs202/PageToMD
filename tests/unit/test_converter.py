"""Unit tests for :mod:`pagetomd.converter`."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from pagetomd.config import Config
from pagetomd.converter import WIDE_TABLE_COL_THRESHOLD, convert
from pagetomd.exceptions import ConversionError
from tests.conftest import make_config


def test_empty_input_raises_conversion_error() -> None:
    """Empty or whitespace-only HTML is a typed conversion failure."""
    with pytest.raises(ConversionError) as excinfo:
        convert("   \n  ", make_config())
    assert excinfo.value.context["html_length"] == 0


def test_basic_paragraph_round_trip() -> None:
    """A single ``<p>`` renders as its text plus a trailing newline."""
    assert convert("<p>hello</p>", make_config()).strip() == "hello"


@pytest.mark.parametrize(
    ("level", "expected_prefix"),
    [
        (1, "# "),
        (2, "## "),
        (3, "### "),
        (4, "#### "),
        (5, "##### "),
        (6, "###### "),
    ],
)
def test_atx_headings_emit_correct_hash_counts(level: int, expected_prefix: str) -> None:
    """Each H1..H6 renders with the matching number of ``#`` characters."""
    html = f"<h{level}>Heading</h{level}>"
    md = convert(html, make_config(heading_style="atx")).strip()
    assert md.startswith(expected_prefix), md
    assert md.endswith("Heading"), md


def test_setext_headings_underline_h1_h2_only() -> None:
    """H1/H2 get underline form; H3+ stays ATX."""
    md = convert(
        "<h1>One</h1><h2>Two</h2><h3>Three</h3><h4>Four</h4>",
        make_config(heading_style="setext"),
    )
    # H1 underlined with '='; H2 underlined with '-'.
    assert "One\n===" in md or "One\n==" in md  # underline length = len(text)
    assert "Two\n---" in md or "Two\n--" in md
    # H3+ remain ATX.
    assert "### Three" in md
    assert "#### Four" in md


def test_fenced_code_with_language_class_python() -> None:
    """``language-python`` becomes the fence info string."""
    md = convert(
        '<pre><code class="language-python">x = 1\nprint(x)</code></pre>',
        make_config(),
    )
    assert "```python\n" in md
    assert "x = 1" in md
    assert "print(x)" in md
    # Closing fence with no info string.
    assert "\n```\n" in md


def test_fenced_code_with_lang_prefix() -> None:
    """``lang-rust`` is recognised as the fallback prefix."""
    md = convert(
        '<pre><code class="lang-rust">fn main(){}</code></pre>',
        make_config(),
    )
    assert "```rust\n" in md
    assert "fn main(){}" in md


def test_fenced_code_with_highlight_prefix() -> None:
    """The ``highlight-xxx`` variant also yields a language hint."""
    md = convert(
        '<pre><code class="highlight-go">package main</code></pre>',
        make_config(),
    )
    assert "```go\n" in md


def test_fenced_code_without_language_class_has_empty_info_string() -> None:
    """No recognisable language class → empty info string after the fence."""
    md = convert("<pre><code>just text</code></pre>", make_config())
    # The fence opens with ``` immediately followed by a newline.
    assert "```\n" in md
    assert "just text" in md


def test_inline_code_uses_backticks() -> None:
    """Plain inline ``<code>`` becomes single-backtick syntax."""
    md = convert("<p>see <code>foo</code> here</p>", make_config()).strip()
    assert md == "see `foo` here"


def test_inline_code_with_internal_backticks_doubles_fence() -> None:
    """When inline code contains a backtick we widen the delimiter."""
    md = convert("<p><code>a`b</code></p>", make_config()).strip()
    # markdownify pads with a space and uses 2 backticks because input has 1.
    assert "``" in md
    assert "a`b" in md


def test_narrow_table_renders_as_gfm_pipe() -> None:
    """A 3-column table stays well under the wide threshold."""
    html = (
        "<table>"
        "<tr><th>A</th><th>B</th><th>C</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr>"
        "</table>"
    )
    md = convert(html, make_config())
    assert "| A | B | C |" in md
    assert "| --- | --- | --- |" in md
    assert "| 1 | 2 | 3 |" in md


def _wide_table_html(cols: int = 6) -> str:
    """Build a wide table with ``cols`` header/body cells for the policy tests."""
    headers = "".join(f"<th>H{i}</th>" for i in range(1, cols + 1))
    body = "".join(f"<td>v{i}</td>" for i in range(1, cols + 1))
    return f"<table><tr>{headers}</tr><tr>{body}</tr></table>"


def _tei_wide_table(cols: int = 6) -> str:
    """Build a TEI-shaped wide table with ``cols`` header + body cells."""
    head_cells = "".join(f'<cell role="head">H{i}</cell>' for i in range(1, cols + 1))
    body_cells = "".join(f"<cell>v{i}</cell>" for i in range(1, cols + 1))
    return f'<table><row span="{cols}">{head_cells}</row><row>{body_cells}</row></table>'


def _tei_narrow_table(cols: int = 3) -> str:
    """Build a TEI-shaped narrow table for the narrow-fallback path."""
    head_cells = "".join(f'<cell role="head">H{i}</cell>' for i in range(1, cols + 1))
    body_cells = "".join(f"<cell>v{i}</cell>" for i in range(1, cols + 1))
    return f"<table><row>{head_cells}</row><row>{body_cells}</row></table>"


@pytest.mark.parametrize(
    ("table_html", "table_type"),
    [
        (_wide_table_html(cols=6), "html"),
        (_tei_wide_table(cols=6), "tei"),
    ],
    ids=["html", "tei"],
)
@pytest.mark.parametrize(
    ("mode", "assertions"),
    [
        (
            "kv",
            lambda md: "### Row 1" in md and "- **H1**: v1" in md and "| H1 |" not in md,
        ),
        (
            "html",
            lambda md: "<table" in md and "<row" not in md and "<cell" not in md,
        ),
        (
            "drop",
            lambda md: "<!-- pagetomd: wide table dropped" in md and "v1" not in md,
        ),
    ],
    ids=["kv", "html", "drop"],
)
def test_wide_table_mode(
    table_html: str, table_type: str, mode: str, assertions: Callable[[str], bool]
) -> None:
    """Wide tables in both HTML and TEI formats are correctly handled by all modes."""
    md = convert(table_html, make_config(wide_tables=mode))
    assert assertions(md)


def test_narrow_table_unaffected_by_wide_table_mode() -> None:
    """Tables at or under the threshold ignore the wide-table policy."""
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    md = convert(html, make_config(wide_tables="drop"))
    assert "| A | B |" in md
    assert "wide table dropped" not in md


def test_images_stripped_when_include_images_false() -> None:
    """``include_images=False`` removes Markdown image syntax entirely."""
    md = convert(
        '<p>before<img alt="x" src="y">after</p>',
        make_config(include_images=False),
    )
    assert "![x]" not in md
    assert "(y)" not in md
    assert "before" in md
    assert "after" in md


def test_images_kept_when_include_images_true() -> None:
    """Default config keeps Markdown image syntax with alt + src."""
    md = convert('<p><img alt="x" src="y"></p>', make_config(include_images=True))
    assert "![x](y)" in md


def test_links_stripped_when_include_links_false() -> None:
    """Anchor text survives, URL does not."""
    md = convert(
        '<p><a href="https://example.com/x">label</a></p>',
        make_config(include_links=False),
    ).strip()
    assert md == "label"


def test_links_kept_when_include_links_true() -> None:
    """Default config keeps Markdown link syntax."""
    md = convert(
        '<p><a href="https://example.com/x">label</a></p>',
        make_config(include_links=True),
    )
    assert "[label](https://example.com/x)" in md


def test_blank_line_normalisation_collapses_runs() -> None:
    """Multiple ``<br>`` chains would otherwise leave 4+ blank lines."""
    # Two adjacent code blocks force markdownify to emit several blank lines.
    html = "<p>a</p><pre><code>x</code></pre><pre><code>y</code></pre><p>b</p>"
    md = convert(html, make_config())
    # Find the longest run of consecutive newlines.
    longest = max(len(run) for run in md.split("a") if "\n" in run)
    # 3 newlines == 2 blank lines, the cap. We assert no run exceeds that.
    assert "\n\n\n\n" not in md, f"unexpected 4+ blank lines:\n{md!r}"
    assert longest >= 2  # at least one paragraph break survived


def test_markdownify_failure_wrapped_as_conversion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception inside ``MarkdownConverter.convert`` becomes typed."""
    from pagetomd import converter as converter_mod

    def _boom(self: object, _html: str) -> str:
        raise RuntimeError("markdownify exploded")

    monkeypatch.setattr(converter_mod.PagetomdConverter, "convert", _boom)

    with pytest.raises(ConversionError) as excinfo:
        convert("<p>hi</p>", make_config())

    assert "markdownify" in str(excinfo.value)
    assert excinfo.value.context["original"] == "markdownify exploded"





def test_empty_pre_returns_empty_string() -> None:
    """An empty ``<pre>`` produces no fenced block."""
    md = convert("<p>before</p><pre></pre><p>after</p>", make_config())
    assert "```" not in md
    assert "before" in md and "after" in md


def test_language_class_on_pre_tag_itself() -> None:
    """When the class lives on the ``<pre>`` (no ``<code>`` inside), we still find it."""
    md = convert('<pre class="language-yaml">a: 1</pre>', make_config())
    assert "```yaml\n" in md


def test_kv_mode_skips_row_with_no_cells() -> None:
    """Empty ``<tr>`` rows inside a wide table are skipped silently."""
    html = (
        "<table>"
        "<tr><th>A</th><th>B</th><th>C</th><th>D</th><th>E</th><th>F</th></tr>"
        "<tr></tr>"
        "<tr><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td><td>6</td></tr>"
        "</table>"
    )
    md = convert(html, make_config(wide_tables="kv"))
    assert "- **A**: 1" in md
    # Only one numbered row should appear (the empty <tr> doesn't earn one).
    assert md.count("### Row") == 1


def test_kv_mode_with_extra_body_cell_uses_col_fallback() -> None:
    """Body cells past the header count fall back to ``colN`` labels."""
    html = (
        "<table>"
        "<tr><th>A</th><th>B</th><th>C</th><th>D</th><th>E</th><th>F</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td><td>6</td><td>X</td></tr>"
        "</table>"
    )
    md = convert(html, make_config(wide_tables="kv"))
    assert "- **col7**: X" in md


def test_empty_table_with_wide_threshold_falls_through() -> None:
    """A ``<table>`` with no rows can't be wide, so default rendering wins."""
    md = convert("<p>a</p><table></table><p>b</p>", make_config(wide_tables="drop"))
    # No wide-table marker should appear.
    assert "wide table dropped" not in md
    assert "a" in md and "b" in md


def test_kv_mode_with_no_rows_returns_empty_string() -> None:
    """``_render_table_as_kv`` on a no-row table is a no-op."""
    from bs4 import BeautifulSoup

    from pagetomd.converter import PagetomdConverter

    soup = BeautifulSoup("<table></table>", "lxml")
    table = soup.find("table")
    converter = PagetomdConverter()
    converter.pagetomd_config = make_config()  # type: ignore[assignment]
    assert table is not None
    assert converter._render_table_as_kv(table) == ""




def test_tei_narrow_table_renders_as_gfm_pipe() -> None:
    """A 3-column TEI table stays narrow and renders as a GFM pipe table."""
    md = convert(_tei_narrow_table(cols=3), make_config())
    assert "| H1 | H2 | H3 |" in md
    assert "| --- | --- | --- |" in md
    assert "| v1 | v2 | v3 |" in md


def test_table_columns_counts_max_across_first_three_rows() -> None:
    """A leading 1-cell row above the header row is ignored."""
    from bs4 import BeautifulSoup

    from pagetomd.converter import _table_columns

    html = (
        "<table>"
        '<tr><td colspan="6">title</td></tr>'  # decorative top row
        "<tr><th>A</th><th>B</th><th>C</th><th>D</th><th>E</th><th>F</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td><td>4</td><td>5</td><td>6</td></tr>"
        "</table>"
    )
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    assert table is not None
    assert _table_columns(table) == 6


def test_marker_comment_inside_code_picks_up_language() -> None:
    """``<!--pagetomd-lang:rust-->`` inside ``<code>`` yields a fenced ``rust`` block."""
    md = convert(
        "<pre><code><!--pagetomd-lang:rust-->fn main() {}</code></pre>",
        make_config(),
    )
    assert "```rust\n" in md
    assert "fn main()" in md


def test_data_lang_on_pre_picks_up_language() -> None:
    """``data-lang`` on the ``<pre>`` is preferred when ``<code>`` carries no class."""
    md = convert(
        '<pre data-lang="elixir"><code>IO.puts "hi"</code></pre>',
        make_config(),
    )
    assert "```elixir\n" in md


def test_text_sentinel_inside_code_picks_up_language_and_strips_marker() -> None:
    """The sentinel text is recognised AND removed from the rendered body."""
    from pagetomd.extractor import LANG_SENTINEL_PREFIX, LANG_SENTINEL_SUFFIX

    sentinel = f"{LANG_SENTINEL_PREFIX}python{LANG_SENTINEL_SUFFIX}"
    md = convert(
        f"<pre><code>{sentinel}\nx = 1\n</code></pre>",
        make_config(),
    )
    assert "```python\n" in md
    # The raw sentinel must not appear in the rendered body.
    assert sentinel not in md


def test_marker_comment_takes_precedence_over_data_lang() -> None:
    """When both signals exist, the comment marker wins (it's earlier in the cascade)."""
    md = convert(
        '<pre data-lang="java"><code><!--pagetomd-lang:kotlin-->fun main(){}</code></pre>',
        make_config(),
    )
    assert "```kotlin\n" in md
    assert "java" not in md.splitlines()[0:5]  # not in the fence


_WIDE_HEADERS = "".join(f"<th>H{i}</th>" for i in range(1, 7))


def _wide_html_table(row_cells_html: str) -> str:
    """Build a 6-column HTML table whose data row contains ``row_cells_html``."""
    return f"<table><tr>{_WIDE_HEADERS}</tr><tr>{row_cells_html}</tr></table>"


@pytest.mark.parametrize(
    ("row_cells_html", "forbidden_strings"),
    [
        (
            '<td onclick="alert(1)">a</td><td>b</td><td>c</td><td>d</td><td>e</td><td>f</td>',
            ["onclick", "alert(1)"],
        ),
        (
            '<td OnError="x">a</td><td OnMouseOver="y">b</td><td>c</td><td>d</td><td>e</td><td>f</td>',
            ["onerror", "onmouseover", '"x"', '"y"'],
        ),
        (
            '<td><a href="javascript:alert(1)">click</a></td><td>b</td><td>c</td><td>d</td><td>e</td><td>f</td>',
            ["javascript:", "alert(1)"],
        ),
        (
            '<td><a href="JAVASCRIPT:alert(1)">click</a></td><td>b</td><td>c</td><td>d</td><td>e</td><td>f</td>',
            ["javascript:", "alert(1)"],
        ),
    ],
    ids=["onclick", "capitalised_handlers", "javascript_href", "uppercase_javascript_href"],
)
def test_wide_html_table_strips_dangerous_attributes(
    row_cells_html: str, forbidden_strings: list[str]
) -> None:
    """Dangerous attributes and javascript: hrefs are stripped from wide HTML tables."""
    html = _wide_html_table(row_cells_html)
    md = convert(html, make_config(wide_tables="html"))
    lower = md.lower()
    for s in forbidden_strings:
        assert s.lower() not in lower


def test_wide_html_table_preserves_benign_https_link() -> None:
    """A normal ``https://`` ``href`` round-trips untouched."""
    html = _wide_html_table(
        '<td><a href="https://example.com">link</a></td>'
        "<td>b</td><td>c</td><td>d</td><td>e</td><td>f</td>"
    )
    md = convert(html, make_config(wide_tables="html"))
    assert 'href="https://example.com"' in md
    assert "link" in md


def test_tei_wide_table_html_mode_strips_event_handlers() -> None:
    """TEI ``<cell onclick=...>`` rows are scrubbed in the TEI→HTML branch."""
    headers = "".join(f'<cell role="head">H{i}</cell>' for i in range(1, 7))
    body_cells = (
        '<cell onclick="alert(1)">a</cell><cell>b</cell><cell>c</cell>'
        "<cell>d</cell><cell>e</cell><cell>f</cell>"
    )
    html = f"<table><row>{headers}</row><row>{body_cells}</row></table>"
    md = convert(html, make_config(wide_tables="html"))
    assert "onclick" not in md.lower()
    assert "alert(1)" not in md
