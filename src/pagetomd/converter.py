"""HTML → Markdown conversion for :mod:`pagetomd`.

Converts cleaned article HTML to Markdown via a :class:`markdownify.MarkdownConverter`
subclass with overrides for code fences, wide tables, images, links, and heading style.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from bs4 import BeautifulSoup, Comment
from bs4.element import NavigableString, Tag
from markdownify import ATX, UNDERLINED, MarkdownConverter

from pagetomd.exceptions import ConversionError
from pagetomd.extractor import (
    LANG_SENTINEL_PREFIX,
    LANG_SENTINEL_SUFFIX,
    match_lang_class,
)
from pagetomd.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only used for type hints
    from pagetomd.config import Config

__all__ = ["WIDE_TABLE_COL_THRESHOLD", "PagetomdConverter", "convert"]


# A table wider than this many columns triggers the wide-table policy
# (``kv`` / ``html`` / ``drop``). GFM pipe tables become unreadable past
# 5 columns, which is why the threshold lives here and not in ``Config``.
WIDE_TABLE_COL_THRESHOLD: Final[int] = 5

# Sentinel pattern used to detect the language hint embedded as a text
# node during pre-clean. Anchored to the start of the code body so we
# only match the intentional prefix, never a coincidental occurrence
# later in the source.
_SENTINEL_LINE_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{re.escape(LANG_SENTINEL_PREFIX)}([\w+\-.]+){re.escape(LANG_SENTINEL_SUFFIX)}\n?"
)

# Pattern recognising the marker comment shape ``pagetomd-lang:<lang>``.
_MARKER_COMMENT_RE: Final[re.Pattern[str]] = re.compile(r"^\s*pagetomd-lang:([\w+\-.]+)\s*$")

# Collapse runs of 3+ blank lines to exactly two blank lines. The
# post-process module does heavier normalisation; this is just so direct
# converter output is not grotesque.
_MULTI_BLANK_LINE_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")

# Tags that count as "header" cells in either HTML or TEI table flavour.
_HEADER_CELL_NAMES: Final[frozenset[str]] = frozenset({"th"})
_DATA_CELL_NAMES: Final[frozenset[str]] = frozenset({"td"})

# Ancestor element names that put us inside *phrasing* (inline) context.
# When a ``<pre>`` lives inside one of these, trafilatura has almost
# certainly upgraded an inline ``<code>`` span to a ``<pre>`` block —
# rendering that as a fenced block fractures the surrounding paragraph
# and triggers MD031 / MD040 lints. We collapse such ``<pre>`` elements
# to inline backtick spans instead, which is both visually correct and
# friendlier to downstream Markdown linters.
_INLINE_ANCESTOR_NAMES: Final[frozenset[str]] = frozenset(
    {"p", "li", "td", "th", "cell", "a", "span", "em", "strong", "b", "i", "u", "small"}
)

# Attributes whose value, after lowercase + strip, starting with any of these
# are scrubbed when emitting wide-table HTML passthrough.
_DANGEROUS_URL_SCHEMES: Final[tuple[str, ...]] = (
    "javascript:",
    "vbscript:",
    "data:text/html",
)

# Attribute names that may carry a URL value worth scrubbing.
_URL_ATTRS: Final[frozenset[str]] = frozenset({"href", "src", "xlink:href", "formaction"})

_log = get_logger(__name__)


def _chomp(text: str) -> tuple[str, str, str]:
    """Strip leading/trailing whitespace, returning ``(prefix, stripped, suffix)``.

    Vendored equivalent of ``markdownify.chomp`` to avoid depending on a
    private/undocumented symbol that is not in ``markdownify.__all__``.

    Args:
        text: Raw text that may contain surrounding whitespace characters.

    Returns:
        A three-tuple ``(prefix, stripped, suffix)`` where *prefix* and
        *suffix* are each either ``" "`` (when the original text started/ended
        with whitespace) or ``""`` (when it did not), and *stripped* is the
        text after calling :py:meth:`str.strip`.
    """
    prefix = " " if text and text[0] in " \t\r\n" else ""
    suffix = " " if text and text[-1] in " \t\r\n" else ""
    text = text.strip()
    return prefix, suffix, text


class PagetomdConverter(MarkdownConverter):
    """Markdownify subclass that injects pagetomd's per-tag rules."""

    pagetomd_config: Config

    def convert_pre(self, el: Tag, text: str, parent_tags: set[str]) -> Any:
        """Render ``<pre>`` as a fenced code block with an optional language."""
        if _is_pre_wrapper(el):
            return text

        language = self._derive_code_language(el)
        sentinel_lang, stripped_text = _extract_sentinel_lang(text)
        if sentinel_lang and not language:
            language = sentinel_lang

        if _has_inline_ancestor(el):
            # Demote back to inline backtick span (trafilatura promotes
            # inline <code> to <pre> in phrasing contexts).
            inline_text = el.get_text(" ", strip=True)
            _, stripped_inline = _extract_sentinel_lang(inline_text)
            inline_text = stripped_inline.strip()
            if not inline_text:
                return ""
            fence = _shortest_inline_fence(inline_text)
            return f"{fence}{inline_text}{fence}"

        body = stripped_text.rstrip()
        if not body:
            return ""

        # Two blank lines around so the fence stays a block, never an
        # inline fragment glued to neighbouring text.
        return f"\n\n```{language}\n{body}\n```\n\n"

    @staticmethod
    def _derive_code_language(pre_el: Tag) -> str:
        """Return the language suffix (no ``language-`` prefix) or ``""``.

        Tries (in order):

        1. The marker comment ``<!--pagetomd-lang:X-->`` embedded as the
           first child of ``<code>`` (or ``<pre>``) by the extractor's
           pre-clean step.
        2. The ``data-lang`` attribute on the ``<pre>`` itself.
        3. ``class="language-X"`` / ``lang-X`` / ``highlight-X`` on the
           first child ``<code>``.
        4. The same class shapes on the ``<pre>`` itself.
        """
        code_el = pre_el.find("code")
        targets: list[Tag] = []
        if isinstance(code_el, Tag):
            targets.append(code_el)
        targets.append(pre_el)

        # 1. Marker comment.
        for target in targets:
            for child in target.children:
                if isinstance(child, Comment):
                    match = _MARKER_COMMENT_RE.match(str(child))
                    if match:
                        return match.group(1).lower()
                # Stop scanning at the first non-whitespace, non-comment
                # text node so we don't pick up a comment embedded deep
                # inside the body.
                if (
                    isinstance(child, NavigableString)
                    and not isinstance(child, Comment)
                    and str(child).strip()
                ):
                    break

        # 2. ``data-lang`` on the ``<pre>``.
        raw_data_lang = pre_el.get("data-lang")
        if isinstance(raw_data_lang, str) and raw_data_lang.strip():
            return raw_data_lang.strip().lower()

        # 3 + 4. Class-based fallbacks.
        for target in targets:
            lang = match_lang_class(target)
            if lang is not None:
                return lang
        return ""

    def convert_table(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        """Render tables, escalating to wide-table policy when needed."""
        cols = _table_columns(el)
        mode = self.pagetomd_config.wide_tables
        if cols > WIDE_TABLE_COL_THRESHOLD:
            if mode == "kv":
                return self._render_table_as_kv(el)
            if mode == "html":
                # The HTML mode emits the table verbatim. TEI tables get
                # transformed back into proper HTML first so the embedded
                # block actually renders in a browser / Markdown viewer.
                # Scrub inline JS hazards (event handlers and
                # ``javascript:`` URLs) before the verbatim HTML escapes
                # into the Markdown stream. The TEI branch is scrubbed
                # inside ``_tei_table_to_html`` so the new tree is
                # cleaned before serialisation.
                if _is_tei_table(el):
                    html_form = _tei_table_to_html(el)
                else:
                    _scrub_passthrough_html(el)
                    html_form = str(el).strip()
                return f"\n\n{html_form}\n\n"
            if mode == "drop":
                return f"\n\n<!-- pagetomd: wide table dropped (cols={cols}) -->\n\n"
        # Narrow tables fall through to markdownify. For TEI input that
        # path produces no useful pipe table because markdownify does not
        # know how to recurse into ``<row>``/``<cell>`` — so we render
        # the GFM pipe table ourselves from the TEI structure.
        if _is_tei_table(el):
            return self._render_tei_table_as_gfm(el)
        return super().convert_table(el, text, parent_tags)  # type: ignore[no-any-return,misc]

    def _render_tei_table_as_gfm(self, table_el: Tag) -> str:
        """Render a TEI ``<table>`` as a GitHub-flavoured pipe table."""
        rows = _iter_rows(table_el)
        if not rows:
            return ""

        # Header row: the first row containing ``<cell role="head">``,
        # falling back to the first row when no role hint exists.
        header_row = next(
            (row for row in rows if any(_is_header_cell(c) for c in _row_cells(row))),
            rows[0],
        )
        headers = [self._cell_text(c) for c in _row_cells(header_row)]
        body_rows = [row for row in rows if row is not header_row]

        if not headers:
            # Defensive: no cells anywhere → emit nothing rather than a
            # malformed pipe table.
            return ""

        lines: list[str] = []
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for row in body_rows:
            cells = [self._cell_text(c) for c in _row_cells(row)]
            # Right-pad / truncate cells to the header width so the
            # rendered table stays rectangular even if a row is short.
            cells = (cells + [""] * len(headers))[: len(headers)]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n\n" + "\n".join(lines) + "\n\n"

    def _render_table_as_kv(self, table_el: Tag) -> str:
        """Render a wide table as a bulleted ``**header**: value`` list."""
        rows = list(_iter_rows(table_el))
        if not rows:
            return ""

        header_row = next(
            (row for row in rows if any(_is_header_cell(c) for c in _row_cells(row))),
            rows[0],
        )
        header_cells = _row_cells(header_row)
        headers = [self._cell_text(c) for c in header_cells]

        body_rows = [row for row in rows if row is not header_row]

        lines: list[str] = ["\n"]
        for idx, row in enumerate(body_rows, start=1):
            body_cells = _row_cells(row)
            if not body_cells:
                continue
            lines.append(f"### Row {idx}\n")
            for col_idx, cell in enumerate(body_cells):
                header = headers[col_idx] if col_idx < len(headers) else f"col{col_idx + 1}"
                value = self._cell_text(cell)
                lines.append(f"- **{header}**: {value}")
            lines.append("")  # blank line between rows
        lines.append("")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _cell_text(cell: Tag) -> str:
        """Flatten a table cell to a single whitespace-collapsed line."""
        mutations: list[tuple[Tag, NavigableString]] = []
        for code_like in cell.find_all(["pre", "code"]):
            if not isinstance(code_like, Tag):  # pragma: no cover - defensive
                continue
            inline = code_like.get_text(" ", strip=True)
            fence = _shortest_inline_fence(inline)
            replacement = NavigableString(f"{fence}{inline}{fence}")
            mutations.append((code_like, replacement))
            code_like.replace_with(replacement)

        text = cell.get_text(separator=" ", strip=True)

        for original, replacement in reversed(mutations):
            replacement.replace_with(original)

        return re.sub(r"\s+", " ", text)

    def convert_code(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        """Render inline ``<code>``, widening the backtick fence if needed."""
        if el.parent and el.parent.name == "pre":
            return text

        prefix, suffix, text = _chomp(text)
        if not text:
            return ""

        if "`" in text:
            fence = _shortest_inline_fence(text)
            return f"{prefix}{fence} {text} {fence}{suffix}"

        return f"{prefix}`{text}`{suffix}"

    def convert_img(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        """Strip images entirely when ``include_images`` is false."""
        if not self.pagetomd_config.include_images:
            return ""
        return super().convert_img(el, text, parent_tags)  # type: ignore[no-any-return,misc]

    def convert_a(self, el: Tag, text: str, parent_tags: set[str]) -> str:
        """Drop the URL but keep the text when ``include_links`` is false."""
        if not self.pagetomd_config.include_links:
            return text
        return super().convert_a(el, text, parent_tags)  # type: ignore[no-any-return,misc]


def convert(html: str, config: Config) -> str:
    """Convert cleaned HTML to a Markdown body string.

    Args:
        html: The cleaned HTML produced by
            :func:`pagetomd.extractor.extract` (or any other source that
            yields well-formed article HTML).
        config: Active :class:`~pagetomd.config.Config`. The converter reads
            ``include_images``, ``include_links``, ``heading_style``, and
            ``wide_tables``.

    Returns:
        A Markdown string. Trailing whitespace is trimmed and runs of 3+
        consecutive blank lines are collapsed to two.

    Raises:
        ConversionError: When the input is empty/whitespace, or when
            markdownify raises while walking the tree.
    """
    if not html or not html.strip():
        raise ConversionError("Cannot convert empty HTML")

    heading_style = UNDERLINED if config.heading_style == "setext" else ATX
    options: dict[str, Any] = {
        "bullets": "-",
        "strip": ["script", "style"],
        "escape_underscores": False,
        "code_language": "",
        "heading_style": heading_style,
    }

    converter = PagetomdConverter(**options)
    # Stash the config on the instance so per-tag overrides can read it
    # without polluting the markdownify ``options`` dict (which would
    # otherwise reject unknown keys).
    converter.pagetomd_config = config

    try:
        markdown = converter.convert(html)
    except Exception as exc:
        raise ConversionError(
            "markdownify failed to convert HTML to Markdown",
        ) from exc

    markdown = _normalise_blank_lines(markdown).strip() + "\n"
    _log.info(
        "convert.ok",
        wide_tables_mode=config.wide_tables,
    )
    return markdown


def _normalise_blank_lines(text: str) -> str:
    """Collapse runs of 3+ blank lines to exactly two newlines.

    Heavier normalisation (NFC, heading hierarchy, trailing spaces) lives
    in :mod:`pagetomd.postprocess`; this is the bare minimum so the
    converter's direct output is not visually broken.
    """
    return _MULTI_BLANK_LINE_RE.sub("\n\n", text)


def _is_tei_table(table: Tag) -> bool:
    """Return ``True`` when ``table`` uses TEI ``<row>``/``<cell>`` markup."""
    return table.find("row") is not None and table.find("tr") is None


def _iter_rows(table: Tag) -> list[Tag]:
    """Yield rows as :class:`Tag` instances regardless of HTML/TEI flavour."""
    rows: list[Tag] = []
    for row in table.find_all(["tr", "row"]):
        if isinstance(row, Tag):
            rows.append(row)
    return rows


def _row_cells(row: Tag) -> list[Tag]:
    """Return the cells of ``row``, accepting either HTML or TEI shapes."""
    return [c for c in row.find_all(["th", "td", "cell"]) if isinstance(c, Tag)]


def _is_header_cell(cell: Tag) -> bool:
    """``True`` for ``<th>`` or TEI ``<cell role="head">`` cells."""
    if cell.name in _HEADER_CELL_NAMES:
        return True
    if cell.name == "cell":
        role = cell.get("role")
        if isinstance(role, str) and role.strip().lower() == "head":
            return True
    return False


def _table_columns(table_el: Tag) -> int:
    """Return the column count (max cells across the first three rows)."""
    counts: list[int] = []
    for row in _iter_rows(table_el)[:3]:
        cells = _row_cells(row)
        if cells:
            counts.append(len(cells))
    return max(counts) if counts else 0


def _tei_table_to_html(table: Tag) -> str:
    """Return ``table`` rewritten as standard HTML (deep copy; caller's tree is unchanged)."""
    soup = BeautifulSoup(str(table), "lxml")
    new_table = soup.find("table")
    if not isinstance(new_table, Tag):  # pragma: no cover - defensive
        return str(table)

    for row in new_table.find_all("row"):
        if not isinstance(row, Tag):  # pragma: no cover - defensive
            continue
        row.name = "tr"
        # ``span`` is TEI's column-span attribute; not meaningful in HTML.
        if "span" in row.attrs:
            del row.attrs["span"]

    for cell in new_table.find_all("cell"):
        if not isinstance(cell, Tag):  # pragma: no cover - defensive
            continue
        role = cell.get("role")
        if isinstance(role, str) and role.strip().lower() == "head":
            cell.name = "th"
            del cell.attrs["role"]
        else:
            cell.name = "td"
            if "role" in cell.attrs:
                del cell.attrs["role"]

    # Scrub inline JS hazards before serialising the new tree
    # so the TEI→HTML branch has the same protection as the verbatim
    # HTML branch in ``convert_table``.
    _scrub_passthrough_html(new_table)

    return str(new_table)


def _scrub_passthrough_html(tag: Tag) -> None:
    """Remove inline JS hazards (``on*`` handlers, ``javascript:`` URLs)
    from ``tag`` and descendants.

    Prevents XSS when wide-table HTML is passed through verbatim into Markdown.
    """
    for el in [tag, *tag.find_all(True)]:
        if not isinstance(el, Tag) or not el.attrs:
            continue
        for attr in list(el.attrs.keys()):
            lname = attr.lower()
            if lname.startswith("on"):
                del el.attrs[attr]
                continue
            if lname in _URL_ATTRS:
                value = el.attrs.get(attr)
                if isinstance(value, str):
                    v = value.strip().lower()
                    if any(v.startswith(scheme) for scheme in _DANGEROUS_URL_SCHEMES):
                        del el.attrs[attr]


def _is_pre_wrapper(el: Tag) -> bool:
    """Return ``True`` when ``el`` is a ``<pre>`` whose only child is another ``<pre>``."""
    if el.name != "pre":
        return False
    inner_pres = 0
    for child in el.children:
        if isinstance(child, Tag):
            if child.name == "pre":
                inner_pres += 1
                continue
            return False
        # NavigableString — only whitespace is allowed alongside the
        # inner <pre>. Anything else (real code content) means the
        # outer <pre> is itself the code container.
        if isinstance(child, NavigableString) and str(child).strip():
            return False
    return inner_pres == 1


def _has_inline_ancestor(el: Tag) -> bool:
    """Return ``True`` when ``el`` lives inside phrasing-context markup.

    Walks up the ancestor chain and returns ``True`` as soon as a tag
    in :data:`_INLINE_ANCESTOR_NAMES` is encountered. Stops at the
    document root. Treats ``<pre>`` nested inside another ``<pre>`` as
    block-level (the outer block wins).
    """
    parent = el.parent
    while parent is not None and isinstance(parent, Tag):
        if parent.name == "pre":
            # An outer ``<pre>`` blocks the inline-context demotion —
            # nested ``<pre>``s are a trafilatura quirk where the
            # whole construct is genuinely a block.
            return False
        if parent.name in _INLINE_ANCESTOR_NAMES:
            return True
        parent = parent.parent
    return False


def _shortest_inline_fence(body: str) -> str:
    """Return the shortest backtick run that can fence ``body`` inline.

    A code span's opening / closing fence must be a run of backticks
    that does not appear inside the span itself. We
    scan ``body`` for the longest existing backtick run and return a
    fence one tick longer so the fences are unambiguous.
    """
    longest = 0
    run = 0
    for ch in body:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * (longest + 1)


def _extract_sentinel_lang(text: str) -> tuple[str | None, str]:
    """Strip the leading language sentinel (if any) and return ``(language, body)``."""
    stripped = text.lstrip("\n")
    leading_blank_count = len(text) - len(stripped)
    match = _SENTINEL_LINE_RE.match(stripped)
    if not match:
        return None, text
    lang = match.group(1).lower()
    remainder = stripped[match.end() :]
    return lang, ("\n" * leading_blank_count) + remainder
