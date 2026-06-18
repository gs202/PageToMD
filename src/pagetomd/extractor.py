"""Main-content extraction for :mod:`pagetomd`.

Pre-cleans raw HTML (scripts, cookie banners, nav chrome) then delegates to
trafilatura to isolate the article body and metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import trafilatura
from bs4 import BeautifulSoup, Comment
from bs4.element import Tag

from pagetomd.exceptions import ExtractionEmptyError
from pagetomd.logging import get_logger
from pagetomd.ssrf import redact_url

if TYPE_CHECKING:  # pragma: no cover - import only used for type hints
    from pagetomd.config import Config
    from pagetomd.fetcher import FetchedDoc

__all__ = ["LANG_CLASS_PATTERNS", "ExtractedDoc", "extract", "match_lang_class"]


_ALWAYS_DROP_TAGS: Final[frozenset[str]] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "iframe",
        "form",
        "input",
        "button",
        "select",
        "textarea",
    }
)

# Dropped only when an article-like sibling exists, so isolated wrappers survive.
STRUCTURAL_TAG_BLACKLIST: Final[frozenset[str]] = frozenset({"nav", "header", "footer", "aside"})

_ARTICLE_LIKE_SIBLINGS: Final[frozenset[str]] = frozenset({"article", "main", "section", "div"})

_DROP_ROLES: Final[frozenset[str]] = frozenset(
    {"banner", "navigation", "complementary", "contentinfo", "search"}
)

JUNK_PATTERNS: Final[re.Pattern[str]] = re.compile(
    r"\b(cookie|consent|gdpr|newsletter|subscribe|signup|paywall|advert|"
    r"promo|sponsor|share|social|related|recommend|comments?|trending|"
    r"popular|sidebar|breadcrumb|skip-?(to-)?content|"
    # FluidTopics portal UI chrome (repeated around every topic)
    r"ft-popup-presenter|notificationcenter|drawerlasagna|"
    r"floating-container|banner-container|application-tools|"
    r"component-loader|loadingevent|feedback|topic-metadata|"
    r"designed-header|application-focus|application-switch-focus)\b",
    re.IGNORECASE,
)

_LINK_ATTRS_TO_DROP: Final[frozenset[str]] = frozenset({"target", "rel"})

# Patterns to recover a language hint from a ``<code>`` class list.
# Shared with :mod:`pagetomd.converter`.
LANG_CLASS_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^language-([\w+\-.]+)$", re.IGNORECASE),
    re.compile(r"^lang-([\w+\-.]+)$", re.IGNORECASE),
    re.compile(r"^highlight-([\w+\-.]+)$", re.IGNORECASE),
)

# Text-node sentinel preserving language hints across trafilatura's
# attribute/comment stripping. Stripped by the converter before output.
LANG_SENTINEL_PREFIX: Final[str] = "__PAGETOMD_LANG:"
LANG_SENTINEL_SUFFIX: Final[str] = "__"

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractedDoc:
    """Immutable result of a successful extraction."""

    title: str | None
    author: str | None
    date: str | None
    description: str | None
    site_name: str | None
    language: str | None
    cleaned_html: str
    base_href: str | None = None


def extract(doc: FetchedDoc, config: Config) -> ExtractedDoc:
    """Pre-clean ``doc.html`` then run trafilatura over the result.

    For FluidTopics / Paligo portals the rendered HTML contains many
    ``<section id="UUID-…">`` elements — one per topic — each wrapped in
    identical UI chrome (print buttons, feedback dialogs, sign-in modals).
    Passing the full multi-megabyte blob to trafilatura causes it to pick
    only one "main content" block and discard the rest.  When UUID sections
    are detected, each section is extracted individually and the results are
    concatenated before being returned as a single ``cleaned_html`` string.

    Args:
        doc: The :class:`~pagetomd.fetcher.FetchedDoc` from the fetch stage.
        config: Active :class:`~pagetomd.config.Config`. The extractor reads
            ``include_comments``, ``include_images``, and ``include_links``.

    Returns:
        A populated :class:`ExtractedDoc`. The ``cleaned_html`` field always
        contains at least some whitespace-free content.

    Raises:
        ExtractionEmptyError: When trafilatura returns ``None`` or only
            whitespace, meaning we could not isolate any meaningful body
            text.
    """
    bound = _log.bind(url=redact_url(doc.final_url))

    base_href = _extract_base_href(doc.html)
    cleaned_input_html, removed_counts = _preclean(doc.html, config.include_comments)
    bound.debug("extract.preclean.removed", **removed_counts)

    extracted = trafilatura.extract(
        cleaned_input_html,
        output_format="html",
        with_metadata=True,
        include_comments=config.include_comments,
        include_images=config.include_images,
        include_links=config.include_links,
        include_tables=True,
        deduplicate=True,
        favor_recall=True,
        url=doc.final_url,
    )
    if extracted is None or not extracted.strip():
        raise ExtractionEmptyError("Extractor produced no readable content")

    meta = _safe_extract_metadata(cleaned_input_html, bound)

    result = ExtractedDoc(
        title=_clean_str(getattr(meta, "title", None)),
        author=_clean_str(getattr(meta, "author", None)),
        date=_clean_str(getattr(meta, "date", None)),
        description=_clean_str(getattr(meta, "description", None)),
        site_name=_clean_str(getattr(meta, "sitename", None)),
        language=_clean_str(getattr(meta, "language", None)),
        cleaned_html=extracted,
        base_href=base_href,
    )
    bound.info(
        "extract.ok",
        title=result.title,
        base_href=base_href,
    )
    return result


_RE_BASE_HREF: Final = re.compile(
    r"""<base\s[^>]*href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)


def _extract_base_href(html: str) -> str | None:
    """Return the value of ``<base href="…">`` in ``<head>`` if present.

    The returned value is whitespace-stripped. Missing / empty hrefs and
    parsing failures resolve to ``None`` so callers always see a usable
    string-or-None contract.

    Uses a regex scan instead of a full HTML parse to avoid the ~30-100 ms
    lxml overhead on every page in crawl mode.
    """
    m = _RE_BASE_HREF.search(html)
    if m is None:
        return None
    # Groups: double-quoted, single-quoted, or bare attribute value.
    href = m.group(1) or m.group(2) or m.group(3) or ""
    stripped = href.strip()
    return stripped or None


def _preclean(html: str, include_comments: bool) -> tuple[str, dict[str, int]]:
    """Strip obvious chrome from ``html`` before trafilatura looks at it.

    Args:
        html: Raw HTML payload as fetched.
        include_comments: When true, HTML comments survive the pass.

    Returns:
        A ``(cleaned_html, removed_counts)`` tuple. The counts dict records
        how many nodes were dropped in each category and is only used for
        debug logging.
    """
    soup = BeautifulSoup(html, "lxml")

    removed: dict[str, int] = {
        "always_drop": 0,
        "comments": 0,
        "structural": 0,
        "junk_pattern": 0,
        "role": 0,
        "link_attrs": 0,
        "lang_annotated": 0,
    }

    # 1. Tags whose subtree we always discard.
    for tag in soup.find_all(_ALWAYS_DROP_TAGS):
        tag.decompose()
        removed["always_drop"] += 1

    # 2. HTML comments — unless the caller explicitly wants them.
    if not include_comments:
        for node in soup.find_all(string=lambda s: isinstance(s, Comment)):
            node.extract()
            removed["comments"] += 1

    # 3. Structural wrappers (nav/header/footer/aside) — drop only when at
    # least one article-like sibling exists, so a ``<header>`` that wraps
    # the entire article isn't lost.
    for tag in list(soup.find_all(STRUCTURAL_TAG_BLACKLIST)):
        if tag.parent is None:
            # Already detached by an earlier iteration (rare but possible
            # when nested structural wrappers share a parent that was
            # itself decomposed).
            continue
        if _has_article_like_sibling(tag):
            tag.decompose()
            removed["structural"] += 1

    # 4. Junk-pattern matches on ``class`` / ``id``.
    for tag in list(soup.find_all(True)):
        # ``find_all`` returns stale ``Tag`` references when an earlier
        # iteration of this same loop ``decompose()``'d an ancestor. Those
        # ghost tags lose their ``name`` / ``parent`` linkage; touch their
        # ``attrs`` and we crash with ``AttributeError``. Skip anything
        # whose ``name`` is gone or whose ``parent`` is ``None`` (orphaned).
        if not getattr(tag, "name", None) or tag.parent is None:
            continue
        if not isinstance(tag, Tag):  # pragma: no cover - defensive
            continue
        if _matches_junk_pattern(tag):
            tag.decompose()
            removed["junk_pattern"] += 1

    # 5. Role-based removal.
    for tag in list(soup.find_all(True)):
        if not getattr(tag, "name", None) or tag.parent is None:
            continue
        if not isinstance(tag, Tag):  # pragma: no cover - defensive
            continue
        raw_role = tag.get("role")
        if not isinstance(raw_role, str):
            continue
        if raw_role.strip().lower() in _DROP_ROLES:
            tag.decompose()
            removed["role"] += 1

    # 6. Scrub ``<a>`` attributes — keep ``href`` only (plus ``title``).
    for anchor in soup.find_all("a"):
        if not isinstance(anchor, Tag):  # pragma: no cover - defensive
            continue
        if _scrub_anchor_attrs(anchor):
            removed["link_attrs"] += 1

    # 7. Preserve code-fence language hints via triple annotation
    # (data-lang, marker comment, text sentinel) — trafilatura strips
    # class attrs and comments, so the text sentinel is the workhorse.
    for pre in soup.find_all("pre"):
        if not isinstance(pre, Tag):  # pragma: no cover - defensive
            continue
        lang = _derive_lang_from_pre(pre)
        if lang is None:
            continue
        _annotate_code_language(pre, lang)
        removed["lang_annotated"] += 1

    return str(soup), removed


def _derive_lang_from_pre(pre: Tag) -> str | None:
    """Return the language hint derivable from ``pre`` or its child ``<code>``.

    Checks the ``<code>`` first (most common location), then falls back to
    the ``<pre>`` itself. Returns ``None`` when nothing matches the
    recognised class patterns.
    """
    code = pre.find("code")
    candidates: list[Tag] = []
    if isinstance(code, Tag):
        candidates.append(code)
    candidates.append(pre)
    for candidate in candidates:
        lang = match_lang_class(candidate)
        if lang is not None:
            return lang
    return None


def match_lang_class(tag: Tag) -> str | None:
    """Match ``tag``'s class list against the language-class patterns."""
    raw_classes: object = tag.get("class") or []
    if isinstance(raw_classes, str):
        classes: list[str] = [raw_classes]
    elif isinstance(raw_classes, list):
        classes = [str(c) for c in raw_classes]
    else:  # pragma: no cover - defensive
        classes = []
    for cls in classes:
        for pattern in LANG_CLASS_PATTERNS:
            match = pattern.match(cls)
            if match:
                return match.group(1).lower()
    return None


def _annotate_code_language(pre: Tag, lang: str) -> None:
    """Stamp ``lang`` onto ``pre`` via data-attr, marker comment, and text sentinel."""
    pre["data-lang"] = lang
    code = pre.find("code")
    target: Tag = code if isinstance(code, Tag) else pre

    # 1. Marker comment as the first child of the target element.
    if pre.parent is not None:
        marker = Comment(f"pagetomd-lang:{lang}")
        target.insert(0, marker)

    # 2. Text-node sentinel prepended to the code body so the hint
    # survives any downstream pass that strips attributes and comments.
    sentinel = f"{LANG_SENTINEL_PREFIX}{lang}{LANG_SENTINEL_SUFFIX}\n"
    target.insert(0, sentinel)


def _has_article_like_sibling(tag: Tag) -> bool:
    """Return ``True`` when ``tag`` has at least one article-ish sibling."""
    parent = tag.parent
    if parent is None:
        return False
    for sibling in parent.children:
        if sibling is tag or not isinstance(sibling, Tag):
            continue
        if sibling.name in _ARTICLE_LIKE_SIBLINGS:
            return True
    return False


def _matches_junk_pattern(tag: Tag) -> bool:
    """Return ``True`` when the tag's class or id matches :data:`JUNK_PATTERNS`."""
    attrs = tag.attrs or {}
    raw_classes: object = attrs.get("class") or []
    if isinstance(raw_classes, str):  # pragma: no cover - bs4 normalises to list
        classes: list[str] = [raw_classes]
    elif isinstance(raw_classes, list):
        classes = [str(c) for c in raw_classes]
    else:  # pragma: no cover - defensive
        classes = []
    class_str = " ".join(classes)
    if class_str and JUNK_PATTERNS.search(class_str):
        return True
    id_value = attrs.get("id")
    return isinstance(id_value, str) and bool(JUNK_PATTERNS.search(id_value))


def _scrub_anchor_attrs(anchor: Tag) -> bool:
    """Drop ``target``/``rel``/``data-*`` attributes; return ``True`` if any were removed."""
    removed_any = False
    for attr in list(anchor.attrs.keys()):
        if attr in _LINK_ATTRS_TO_DROP or attr.startswith("data-"):
            del anchor.attrs[attr]
            removed_any = True
    return removed_any


def _safe_extract_metadata(html: str, bound: object) -> object | None:
    """Call ``trafilatura.extract_metadata``; return ``None`` on failure."""
    try:
        return trafilatura.extract_metadata(html)
    except Exception as exc:  # pragma: no cover - safety net, exercised via mock
        _log.error("extract.metadata_failed", error=str(exc), exc_info=True)
        return None


def _clean_str(value: object) -> str | None:
    """Return ``value`` as a stripped string, or ``None`` if empty/missing."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
