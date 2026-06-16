"""Markdown post-processing / normalisation for :mod:`pagetomd`.

Pure-function module that normalises raw markdownify output (NFC, heading
hierarchy, URL rewriting, blank-line collapsing). Idempotence is a hard
requirement. Fenced code blocks are treated as opaque.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final
from urllib.parse import urljoin, urlsplit

from pagetomd.exceptions import ConversionError
from pagetomd.logging import get_logger

__all__ = ["postprocess"]


_ZERO_WIDTH_CODEPOINTS: Final[tuple[int, ...]] = (
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER
    0xFEFF,  # ZERO WIDTH NO-BREAK SPACE (BOM)
    0x2060,  # WORD JOINER
)
_ZERO_WIDTH_TABLE: Final[dict[int, None]] = dict.fromkeys(_ZERO_WIDTH_CODEPOINTS)

_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

_ATX_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

_SETEXT_H1_RE: Final[re.Pattern[str]] = re.compile(r"^=+\s*$")
_SETEXT_H2_RE: Final[re.Pattern[str]] = re.compile(r"^-+\s*$")

_INLINE_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"(!?)\[([^\]\n]*)\]\(([^)\s]+)(\s+\"[^\"]*\"|\s+'[^']*')?\)"
)

_REF_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\[[^\]\n]+\]:\s*)(\S+?)(\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*$"
)

# URL schemes we treat as already-absolute and never rewrite. Note that
# ``javascript`` / ``vbscript`` are deliberately NOT here — they are
# neutralised by :func:`_absolutise` (see :data:`_DANGEROUS_SCHEME_PREFIXES`)
# so an extracted ``[text](javascript:…)`` link cannot smuggle script into
# the output Markdown.
_ABSOLUTE_SCHEMES: Final[frozenset[str]] = frozenset(
    {"http", "https", "mailto", "tel", "data", "ftp", "ftps", "file"}
)

# Dangerous URL-scheme prefixes that must never survive into the output.
# Compared against ``href.strip().lower()`` so obfuscated casing / leading
# whitespace cannot slip past. Matching links are neutralised to an inert
# ``#`` anchor (link text is preserved). Mirrors the scrub list used by the
# wide-table HTML passthrough path in :mod:`pagetomd.converter`.
_DANGEROUS_SCHEME_PREFIXES: Final[tuple[str, ...]] = (
    "javascript:",
    "vbscript:",
    "data:text/html",
)

_MULTI_BLANK_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")

_log = get_logger(__name__)


def postprocess(markdown: str, *, base_url: str, title: str | None = None) -> str:
    """Normalise a Markdown body into the canonical pagetomd output form.

    The transformation is deterministic and idempotent: feeding the result
    back through :func:`postprocess` with the same ``base_url`` and
    ``title`` yields the same string.

    Args:
        markdown: Raw Markdown body, typically produced by
            :func:`pagetomd.converter.convert`. May contain CRLF line
            endings, zero-width characters, malformed heading hierarchy,
            and relative URLs.
        base_url: The URL of the source page (preferably the *final* URL
            after redirects). Used as the base for resolving every
            relative ``href`` / ``src`` via :func:`urllib.parse.urljoin`.
        title: Optional document title. When the input contains zero H1
            headings, a ``# {title}`` line is prepended. When the input
            already has at least one H1, this argument is ignored.

    Returns:
        The normalised Markdown string. Empty input returns ``""``
        (no synthetic trailing newline).

    Raises:
        ConversionError: When an internal step raises an unexpected
            exception. The original exception message is preserved in
            ``context["original"]``.
    """
    if markdown == "":
        return ""

    try:
        text = unicodedata.normalize("NFC", markdown)
        text = text.translate(_ZERO_WIDTH_TABLE)
        # Normalise CRLF to LF first so we can split on \n safely
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        text = "\n".join(line.rstrip() for line in lines)
        text = _MULTI_BLANK_RE.sub("\n\n", text)

        segments = _split_fenced_blocks(text)
        heading_state: dict[str, int] = {"h1_count": 0, "last_level": 0}
        for is_code, segment in segments:
            if not is_code:
                heading_state["h1_count"] += _count_h1s(segment)

        # NFC-normalise the title before embedding so the second idempotence
        # pass sees the same characters. Without this, codepoints that change
        # under NFC (e.g. CJK compatibility ideographs) break idempotence.
        stripped_title = unicodedata.normalize("NFC", title).strip() if title else None
        prepend_title = heading_state["h1_count"] == 0 and bool(stripped_title)
        # Pre-seed state as if the synthetic H1 already exists, so heading
        # normalisation produces idempotent results on both passes.
        h1_seen = 1 if prepend_title else 0
        last_level = 1 if prepend_title else 0
        new_segments: list[str] = []
        for is_code, segment in segments:
            if is_code:
                new_segments.append(segment)
                continue
            normalised, h1_seen, last_level = _normalise_headings(
                segment,
                h1_seen=h1_seen,
                last_level=last_level,
                total_h1s=heading_state["h1_count"],
            )
            normalised = _rewrite_urls(normalised, base_url)
            new_segments.append(normalised)

        text = "".join(new_segments)

        if prepend_title:
            assert stripped_title is not None  # narrow for mypy; guarded above
            text = f"# {stripped_title}\n\n{text.lstrip(chr(10))}"

        text = _MULTI_BLANK_RE.sub("\n\n", text)

        return text.rstrip("\n") + "\n"
    except ConversionError:
        raise
    except Exception as exc:  # pragma: no cover - defensive catch-all
        _log.warning("postprocess_failed", error=str(exc))
        raise ConversionError(
            "Markdown post-processing failed.",
            original=str(exc),
            stage="postprocess",
        ) from exc


def _split_fenced_blocks(text: str) -> list[tuple[bool, str]]:
    """Split ``text`` into alternating ``(is_code, segment_text)`` tuples."""
    lines = text.split("\n")
    segments: list[tuple[bool, str]] = []
    current: list[str] = []
    in_code = False
    fence_char: str | None = None  # backtick or tilde of the opening fence
    fence_len = 0

    for idx, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if not in_code:
            if match:
                if current:
                    segments.append((False, "\n".join(current) + "\n"))
                    current = []
                fence_char = match.group(1)[0]
                fence_len = len(match.group(1))
                in_code = True
                current.append(line)
            else:
                current.append(line)
        else:
            current.append(line)
            if match and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_len:
                is_last_line = idx == len(lines) - 1
                suffix = "" if is_last_line else "\n"
                segments.append((True, "\n".join(current) + suffix))
                current = []
                in_code = False
                fence_char = None
                fence_len = 0

    if current:
        segments.append((in_code, "\n".join(current)))

    return segments


def _count_h1s(segment: str) -> int:
    """Return the number of H1 headings (ATX or setext) in a prose segment."""
    lines = segment.split("\n")
    count = 0
    for i, line in enumerate(lines):
        atx_match = _ATX_RE.match(line)
        if atx_match and len(atx_match.group(1)) == 1:
            count += 1
            continue
        if (
            _SETEXT_H1_RE.match(line)
            and i > 0
            and lines[i - 1].strip()
            and not _ATX_RE.match(lines[i - 1])
        ):
            count += 1
    return count


def _normalise_headings(
    segment: str,
    *,
    h1_seen: int,
    last_level: int,
    total_h1s: int,
) -> tuple[str, int, int]:
    """Apply the heading normalisation rules to a single prose segment.

    The function (a) converts setext headings to ATX, (b) demotes
    duplicate H1s to H2 when more than one H1 exists in the whole
    document, and (c) walks the heading sequence to remove skipped
    levels by promoting deep headings up to ``last_level + 1``.

    Args:
        segment: A prose (non-code) segment of the document.
        h1_seen: H1 count already encountered in earlier segments.
        last_level: Heading level of the most recent heading encountered
            in earlier segments (``0`` if none yet).
        total_h1s: Total H1 count across the entire document.

    Returns:
        Tuple of ``(rewritten_segment, new_h1_seen, new_last_level)``.
    """
    lines = segment.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Setext detection (consumes two lines if matched)
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        if line.strip() and not _ATX_RE.match(line) and not _FENCE_RE.match(line):
            if _SETEXT_H1_RE.match(next_line):
                level, text = 1, line.strip()
                level, h1_seen, last_level = _resolve_level(level, h1_seen, last_level, total_h1s)
                out.append(_emit_atx(level, text, out))
                i += 2
                continue
            if _SETEXT_H2_RE.match(next_line):
                level, text = 2, line.strip()
                level, h1_seen, last_level = _resolve_level(level, h1_seen, last_level, total_h1s)
                out.append(_emit_atx(level, text, out))
                i += 2
                continue

        # ATX heading
        atx_match = _ATX_RE.match(line)
        if atx_match:
            level = len(atx_match.group(1))
            text = atx_match.group(2).strip()
            level, h1_seen, last_level = _resolve_level(level, h1_seen, last_level, total_h1s)
            out.append(_emit_atx(level, text, out))
            i += 1
            continue

        out.append(line)
        i += 1

    return "\n".join(out), h1_seen, last_level


def _resolve_level(
    level: int,
    h1_seen: int,
    last_level: int,
    total_h1s: int,
) -> tuple[int, int, int]:
    """Apply the one-H1 + no-skip rules to a single heading.

    Args:
        level: The raw heading level (1-6).
        h1_seen: H1 count so far.
        last_level: Level of the last heading emitted.
        total_h1s: Total H1 count across the document.

    Returns:
        ``(adjusted_level, new_h1_seen, new_last_level)``.
    """
    # One-H1 rule: when multiple H1s exist, the *first* keeps level 1 and
    # subsequent H1s are demoted to H2.
    if level == 1:
        h1_seen += 1
        if total_h1s > 1 and h1_seen > 1:
            level = 2

    # No-skipped-levels rule: a heading can be at most ``last_level + 1``
    # deep (or level 1 when there is no previous heading). H1 stays H1.
    if last_level == 0:
        # First heading in the document: H1 is allowed; deeper headings
        # are *not* forcibly promoted to H1. Leave them as-is so a doc
        # with a single H3 remains an H3; the "promote" rule fires only
        # after we've seen a shallower heading.
        last_level = level
    else:
        max_allowed = last_level + 1
        if level > max_allowed:
            level = max_allowed
        last_level = level

    return level, h1_seen, last_level


def _emit_atx(level: int, text: str, prior: list[str]) -> str:
    """Return the ATX heading line, preceded by a blank line when needed.

    Args:
        level: Final heading level (1-6).
        text: Heading text, already stripped.
        prior: Already-emitted lines of the current segment. Used to
            decide whether a leading blank line is required so the
            heading is rendered as a block.

    Returns:
        The heading line, optionally prefixed with ``"\\n"`` for blank
        separation from preceding content.
    """
    hashes = "#" * level
    line = f"{hashes} {text}"
    # Headings render as blocks only when separated by a blank line. If
    # the previous output line is non-empty and is not itself the start
    # of the document, inject a blank line before this heading.
    if prior and prior[-1] != "":
        return "\n" + line
    return line


def _rewrite_urls(segment: str, base_url: str) -> str:
    """Resolve relative URLs in a prose segment against ``base_url``.

    Both inline links / images and reference-style link definitions are
    handled. Fragment-only refs (``#section``) and any URL whose scheme is
    in :data:`_ABSOLUTE_SCHEMES` are left untouched.

    Args:
        segment: A prose (non-code) segment.
        base_url: The URL to resolve relative references against.

    Returns:
        ``segment`` with relative URLs rewritten to absolute form.
    """
    if not base_url:  # pragma: no cover - guard for callers without a base URL
        return segment

    def _inline_sub(match: re.Match[str]) -> str:
        bang, text, href, title = match.groups()
        new_href = _absolutise(href, base_url)
        title_part = title or ""
        return f"{bang}[{text}]({new_href}{title_part})"

    segment = _INLINE_LINK_RE.sub(_inline_sub, segment)

    new_lines: list[str] = []
    for line in segment.split("\n"):
        ref_match = _REF_LINK_RE.match(line)
        if ref_match:
            prefix, href, title = ref_match.groups()
            new_href = _absolutise(href, base_url)
            title_part = title or ""
            new_lines.append(f"{prefix}{new_href}{title_part}")
        else:
            new_lines.append(line)
    return "\n".join(new_lines)


def _absolutise(href: str, base_url: str) -> str:
    """Resolve ``href`` against ``base_url`` unless it is already absolute.

    Args:
        href: The raw URL extracted from a Markdown link or image.
        base_url: The base URL of the document.

    Returns:
        The resolved URL, or the original ``href`` when it is a fragment
        ref, empty, or already uses an absolute scheme. Dangerous schemes
        (``javascript:``, ``vbscript:``, ``data:text/html``) are neutralised
        to an inert ``#`` anchor so script cannot reach the output.
    """
    if not href:  # pragma: no cover - inline-link regex disallows empty href
        return href
    if href.startswith("#"):
        return href
    # Neutralise script-bearing schemes so they never reach the output.
    lowered = href.strip().lower()
    if any(lowered.startswith(prefix) for prefix in _DANGEROUS_SCHEME_PREFIXES):
        return "#"
    scheme = urlsplit(href).scheme.lower()
    if scheme in _ABSOLUTE_SCHEMES:
        return href
    return urljoin(base_url, href)
