"""Output writer for :mod:`pagetomd`.

This module is the very last stage of the pipeline: it turns a finished
Markdown *body* plus a populated :class:`Frontmatter` into the final document
on disk (or on stdout). The writer is intentionally I/O-only — all rendering
concerns live upstream, and the writer's job is to put bytes somewhere
correctly, atomically, and with stable formatting.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
import sys
from collections import OrderedDict
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]
from slugify import slugify

from pagetomd.exceptions import WriteError
from pagetomd.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only used for type hints
    from pagetomd.extractor import ExtractedDoc
    from pagetomd.fetcher import FetchedDoc

__all__ = [
    "Frontmatter",
    "build_frontmatter",
    "serialize_frontmatter",
    "slugify_default_path",
    "write_output",
]

# Sentinel that callers (typically the CLI) pass for "write to stdout instead
# of a file". Both ``Path("-")`` and the literal string ``"-"`` are accepted
# so the writer can be invoked from either typer (which yields a Path) or
# from ad-hoc test code.
_STDOUT_SENTINEL: str = "-"

# Maximum slug length — keeps default filenames short enough to remain usable
# across filesystems with 255-byte name limits, even with a ".md" suffix.
_SLUG_MAX_LENGTH: int = 80

# Fallback slug when every other heuristic yields an empty string.
_FALLBACK_SLUG: str = "page"

# Windows reserved device-name stems (case-insensitive). A file named after
# one of these collides with a DOS device on Windows even with a ``.md``
# extension.
_WINDOWS_RESERVED_STEMS: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(0, 10)),
        *(f"lpt{i}" for i in range(0, 10)),
    }
)

# A very high YAML emit width effectively disables line folding so long URLs
# stay on a single line.
_YAML_WIDTH: int = 10_000

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Frontmatter:
    """Immutable bundle of YAML-frontmatter fields.

    Fields set to ``None`` or ``""`` are omitted from the serialised YAML.
    Dataclass field order determines YAML key order.
    """

    url: str
    final_url: str
    title: str | None = None
    author: str | None = None
    date: str | None = None
    description: str | None = None
    site_name: str | None = None
    language: str | None = None
    fetched_at: str | None = None
    tool: str = "pagetomd"
    tool_version: str = ""


def build_frontmatter(
    fetched: FetchedDoc,
    extracted: ExtractedDoc,
    *,
    include_fetched_at: bool = True,
    now: datetime | None = None,
) -> Frontmatter:
    """Assemble a :class:`Frontmatter` from the fetch + extract results.

    Args:
        fetched: The :class:`~pagetomd.fetcher.FetchedDoc` providing ``url``
            and ``final_url``.
        extracted: The :class:`~pagetomd.extractor.ExtractedDoc` providing the
            metadata bundle (title, author, date, ...).
        include_fetched_at: When ``True`` (default), populate
            :attr:`Frontmatter.fetched_at` from ``now``. When ``False``,
            leave it as ``None`` so the field is omitted from the YAML —
            useful for deterministic output (``--no-fetched-at``).
        now: Reference instant for ``fetched_at``. Defaults to
            :func:`datetime.now` in UTC. Exposed so tests can pin a value.

    Returns:
        A populated :class:`Frontmatter` ready for
        :func:`serialize_frontmatter`.
    """
    # Imported here (not at module top) so importing ``pagetomd.writer`` from
    # within ``pagetomd/__init__.py`` would never form a cycle.
    from pagetomd import __version__

    fetched_at: str | None = None
    if include_fetched_at:
        when = now if now is not None else datetime.now(tz=UTC)
        # Normalise to UTC regardless of the input's tzinfo, then format with
        # a trailing "Z" (instead of "+00:00") so the value is the canonical
        # ISO 8601 UTC representation our docs promise.
        fetched_at = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return Frontmatter(
        url=fetched.url,
        final_url=fetched.final_url,
        title=extracted.title,
        author=extracted.author,
        date=extracted.date,
        description=extracted.description,
        site_name=extracted.site_name,
        language=extracted.language,
        fetched_at=fetched_at,
        tool="pagetomd",
        tool_version=__version__,
    )


def serialize_frontmatter(fm: Frontmatter) -> str:
    """Serialise a :class:`Frontmatter` to a delimited YAML block.

    The output is bracketed by ``---`` fences and terminates with a single
    trailing newline, so the block can be concatenated directly with the
    Markdown body.

    Args:
        fm: The frontmatter bundle to serialise.

    Returns:
        A string of the form ``"---\\n<yaml>---\\n"`` where ``<yaml>`` is a
        block-style YAML mapping containing only the non-``None`` fields of
        ``fm``, in dataclass declaration order.
    """
    ordered: OrderedDict[str, object] = OrderedDict()
    for field in fields(fm):
        value = getattr(fm, field.name)
        if value is None or value == "":
            continue
        ordered[field.name] = value

    body = yaml.safe_dump(
        dict(ordered),  # safe_dump still consults insertion order in 3.7+.
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=_YAML_WIDTH,
    )
    return f"---\n{body}---\n"


def slugify_default_path(fetched: FetchedDoc, extracted: ExtractedDoc) -> Path:
    """Derive a default ``*.md`` filename in CWD from page metadata.

    The heuristic prefers the page title; failing that it falls back to the
    last non-empty path segment of the final URL; failing *that* it uses the
    host. If every candidate slugs to the empty string (e.g. an all-emoji
    title with no useful URL path), we fall back to ``"page"`` so we always
    produce a valid filename.

    Args:
        fetched: Source :class:`~pagetomd.fetcher.FetchedDoc` providing the
            final URL to mine for fallback slug material.
        extracted: Source :class:`~pagetomd.extractor.ExtractedDoc` whose
            title is the preferred slug source.

    Returns:
        A relative :class:`pathlib.Path` of the form ``"<slug>.md"`` rooted
        in the current working directory. When the computed stem matches a
        Windows reserved device name (``CON``, ``PRN``, ``AUX``, ``NUL``,
        ``COM0..9``, ``LPT0..9`` — case-insensitive), ``"-page"`` is
        appended to the stem (e.g. ``con.md`` → ``con-page.md``) so the
        default output is safe to create on every supported platform.
    """
    raw_title = (extracted.title or "").strip()
    # Try each candidate in order, falling through whenever slugify yields
    # an empty string. This is the only way to handle titles like "🚀🚀🚀"
    # where the raw string is truthy but slugs down to nothing.
    candidates: list[str] = []
    if raw_title:
        candidates.append(raw_title)
    url_candidate = _slug_candidate_from_url(fetched.final_url)
    if url_candidate:
        candidates.append(url_candidate)

    slug = ""
    for candidate in candidates:
        slug = slugify(
            candidate,
            max_length=_SLUG_MAX_LENGTH,
            lowercase=True,
            word_boundary=True,
        )
        if slug:
            break
    if not slug:
        slug = _FALLBACK_SLUG
    # Windows reserved-name guard: even with the ``.md`` extension,
    # a file literally named ``CON``/``PRN``/``AUX``/``NUL``/``COMn``/``LPTn``
    # collides with a DOS device on Windows. Suffixing with ``-page`` keeps
    # the slug human-readable while sidestepping the OS-level collision.
    if slug.lower() in _WINDOWS_RESERVED_STEMS:
        slug = f"{slug}-page"
    return Path(f"{slug}.md")


def write_output(
    markdown_body: str,
    frontmatter: Frontmatter,
    *,
    output: Path | None,
    overwrite: bool,
    follow_symlinks: bool = False,
) -> Path | None:
    """Render and persist the full document (frontmatter + body).

    The output destination is decided by ``output``:

    * ``Path("-")`` (or the literal string ``"-"``) → write the rendered
      document to :data:`sys.stdout` and return ``None``. ``overwrite`` is
      ignored in this mode.
    * Any other :class:`Path` → atomically write to that path. The parent
      directory is created if missing; an existing destination triggers a
      :class:`~pagetomd.exceptions.WriteError` unless ``overwrite`` is set.

    The atomic strategy writes to a hidden, collision-resistant sibling temp
    file (``.<name>.tmp.<pid>.<rand>``), :func:`os.fsync`'s the file
    descriptor, then :func:`os.replace`'s the temp over the destination. On
    any error the temp file is removed best-effort and the original
    exception is wrapped in :class:`~pagetomd.exceptions.WriteError`.

    Note:
        Symlinked destinations are refused by default. When the target
        exists and :func:`os.lstat` reports a symlink, the writer raises
        :class:`~pagetomd.exceptions.WriteError` regardless of
        ``overwrite``. Pass ``follow_symlinks=True`` to opt back into the
        legacy behaviour where writes replace the link's target.

    Args:
        markdown_body: The post-processed Markdown body. Trailing newlines
            are normalised to exactly one before writing.
        frontmatter: The serialised frontmatter to prepend.
        output: Destination path, or the stdout sentinel ``Path("-")``.
            ``None`` is rejected — the CLI is responsible for resolving
            defaults via :func:`slugify_default_path` before calling here.
        overwrite: When ``True``, overwrite an existing destination file.
        follow_symlinks: When ``False`` (default), a symlinked destination
            is refused with a :class:`~pagetomd.exceptions.WriteError`.
            When ``True``, the legacy behaviour applies and the write
            propagates through the link to its target.

    Returns:
        The destination :class:`pathlib.Path` on a successful file write, or
        ``None`` when output went to stdout.

    Raises:
        WriteError: For any I/O failure, when the destination exists and
            ``overwrite`` is ``False``, when the destination is a symlink
            and ``follow_symlinks`` is ``False``, or when ``output`` is
            ``None``.
    """
    document = _render_document(markdown_body, frontmatter)

    if _is_stdout_sentinel(output):
        sys.stdout.write(document)
        sys.stdout.flush()
        _log.info(
            "write.ok",
            path="stdout",
            bytes_written=len(document.encode("utf-8")),
        )
        return None

    if output is None:
        # Defensive guard: the CLI is contractually required to resolve the
        # default path before calling us, but raise a typed error if it ever
        # forgets so we never silently no-op.
        raise WriteError(
            "Output path was not provided",
            path=None,
            hint="Pass an explicit Path or '-' for stdout.",
        )

    # ``output`` is what we report back to the caller (the path they asked
    # for); ``target`` is what we actually write to. They diverge only when
    # ``follow_symlinks=True`` resolves through a link.
    output_path = output
    target = output

    # Probe the target with lstat (which does NOT follow symlinks) BEFORE
    # the existence check, so a symlink destination is refused regardless
    # of whether ``overwrite`` is set. FileNotFoundError → no symlink to
    # detect → proceed normally; the regular ``exists()`` check below
    # handles the "exists as a regular file" case.
    try:
        link_st = os.lstat(target)
    except FileNotFoundError:
        link_st = None
    if link_st is not None and stat.S_ISLNK(link_st.st_mode):
        if not follow_symlinks:
            raise WriteError(
                "Destination is a symlink. Pass --follow-symlinks to replace via the link target.",
                path=str(target),
            )
        # ``follow_symlinks=True`` → resolve the link to its target so the
        # atomic write replaces the underlying file rather than swapping the
        # link itself (which would silently break the link's identity on
        # the next pass). ``os.path.realpath`` follows ALL links in the
        # chain so we land on the final regular-file target.
        target = Path(os.path.realpath(target))

    if target.exists():
        if not overwrite:
            raise WriteError(
                "Destination file already exists. Use --overwrite to replace.",
                path=str(target),
                hint="Pass --overwrite to replace the existing file.",
            )
        _log.warning("write.overwrite", path=str(target))

    _ensure_parent_dir(target)
    _atomic_write(target, document)

    _log.info(
        "write.ok",
        path=str(target),
        bytes_written=len(document.encode("utf-8")),
    )
    return output_path


def _render_document(markdown_body: str, frontmatter: Frontmatter) -> str:
    """Concatenate serialised frontmatter, a blank line, and the body.

    The body is normalised to end in exactly one ``\\n``.
    """
    head = serialize_frontmatter(frontmatter)
    body = markdown_body.rstrip("\n") + "\n"
    return f"{head}\n{body}"


def _is_stdout_sentinel(output: Path | str | None) -> bool:
    """Return ``True`` when ``output`` is the stdout sentinel ``"-"``."""
    if output is None:
        return False
    return str(output) == _STDOUT_SENTINEL


def _ensure_parent_dir(target: Path) -> None:
    """Create ``target.parent`` if missing; wrap failures in WriteError."""
    parent = target.parent
    if not parent or str(parent) in {"", "."}:
        return
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WriteError(
            f"Failed to create output directory: {exc}",
            path=str(target),
            original=str(exc),
        ) from exc


def _atomic_write(target: Path, document: str) -> None:
    """Write ``document`` to ``target`` atomically.

    The implementation writes to a sibling temp file, fsyncs, then issues an
    :func:`os.replace`. On any error the temp file is best-effort removed
    and the original exception is wrapped in
    :class:`~pagetomd.exceptions.WriteError`.
    """
    temp_name = f".{target.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    temp_path = target.with_name(temp_name)
    try:
        with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise WriteError(
            f"Failed to write output: {exc}",
            path=str(target),
            original=str(exc),
        ) from exc


def _slug_candidate_from_url(url: str) -> str:
    """Return the best slug-source string we can mine from ``url``.

    Tries the last non-empty path segment first; falls back to the host. If
    neither yields anything, returns the empty string so the caller can
    apply its own fallback.
    """
    parts = urlsplit(url)
    path_segments = [seg for seg in parts.path.split("/") if seg]
    if path_segments:
        return path_segments[-1]
    # ``hostname`` may be ``None`` for malformed URLs — defend against it so
    # the slug pipeline always sees a real string.
    return parts.hostname or ""
