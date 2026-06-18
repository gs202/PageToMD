"""Unit tests for :mod:`pagetomd.writer`."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

import pagetomd
from pagetomd.exceptions import WriteError
from pagetomd.extractor import ExtractedDoc
from pagetomd.fetcher import FetchedDoc
from pagetomd.writer import (
    Frontmatter,
    build_frontmatter,
    serialize_frontmatter,
    slugify_default_path,
    write_output,
)
from tests.conftest import make_fetched_doc


def _make_extracted(
    *,
    title: str | None = "Why FastAPI?",
    author: str | None = "Jane Doe",
    date: str | None = "2024-01-15",
    description: str | None = "A short description.",
    site_name: str | None = "Example",
    language: str | None = "en",
) -> ExtractedDoc:
    """Build an :class:`ExtractedDoc` with sane defaults for writer tests."""
    return ExtractedDoc(
        title=title,
        author=author,
        date=date,
        description=description,
        site_name=site_name,
        language=language,
        cleaned_html="<p>body</p>",
    )


def _frontmatter(**overrides: object) -> Frontmatter:
    """Build a :class:`Frontmatter` with sane defaults for writer tests."""
    base: dict[str, object] = {
        "url": "https://example.com/post",
        "final_url": "https://example.com/post",
        "title": "Hello",
        "author": "Alice",
        "date": "2024-01-15",
        "description": "desc",
        "site_name": "Example",
        "language": "en",
        "fetched_at": "2026-06-15T07:30:00Z",
        "tool": "pagetomd",
        "tool_version": "1.2.3",
    }
    base.update(overrides)
    return Frontmatter(**base)  # type: ignore[arg-type]


@pytest.fixture
def chdir_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Change into ``tmp_path`` for the duration of a test."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def test_frontmatter_is_frozen() -> None:
    """:class:`Frontmatter` rejects mutation."""
    fm = _frontmatter()
    with pytest.raises(FrozenInstanceError):
        fm.title = "mutated"  # type: ignore[misc]


def test_build_frontmatter_with_fixed_now() -> None:
    """``fetched_at`` is the exact UTC ISO string of ``now``."""
    fixed = datetime(2026, 6, 15, 7, 30, 0, tzinfo=UTC)
    fm = build_frontmatter(
        make_fetched_doc(url="https://example.com/blog/why-fastapi"),
        _make_extracted(),
        include_fetched_at=True,
        now=fixed,
    )
    assert fm.fetched_at == "2026-06-15T07:30:00Z"


def test_build_frontmatter_no_fetched_at() -> None:
    """``include_fetched_at=False`` → ``fetched_at is None``."""
    fm = build_frontmatter(
        make_fetched_doc(url="https://example.com/blog/why-fastapi"),
        _make_extracted(),
        include_fetched_at=False,
    )
    assert fm.fetched_at is None


def test_build_frontmatter_populates_tool_version() -> None:
    """``tool_version`` matches :data:`pagetomd.__version__`."""
    fm = build_frontmatter(
        make_fetched_doc(url="https://example.com/blog/why-fastapi"),
        _make_extracted(),
    )
    assert fm.tool == "pagetomd"
    assert fm.tool_version == pagetomd.__version__


def test_serialize_omits_none_fields() -> None:
    """``None`` fields produce no key (no ``author: null``)."""
    fm = _frontmatter(author=None, description=None, site_name=None, language=None)
    yaml_text = serialize_frontmatter(fm)
    assert "author:" not in yaml_text
    assert "description:" not in yaml_text
    assert "site_name:" not in yaml_text
    assert "language:" not in yaml_text
    assert "null" not in yaml_text


def test_serialize_preserves_field_order() -> None:
    """Keys appear in dataclass declaration order."""
    fm = _frontmatter()
    text = serialize_frontmatter(fm)
    # Find each known key's index; assert strictly increasing for the order
    # we care about.
    order = ["url:", "final_url:", "title:", "author:", "date:", "description:"]
    positions = [text.index(key) for key in order]
    assert positions == sorted(positions)


def test_serialize_allows_unicode() -> None:
    """Unicode (Hebrew) survives serialisation intact."""
    fm = _frontmatter(title="שלום")
    text = serialize_frontmatter(fm)
    assert "שלום" in text


def test_serialize_does_not_wrap_long_urls() -> None:
    """A 200+ char URL stays on a single line."""
    long_path = "a" * 220
    long_url = f"https://example.com/{long_path}"
    fm = _frontmatter(url=long_url, final_url=long_url)
    text = serialize_frontmatter(fm)
    # The line containing the URL must contain the full URL — i.e. not split.
    matching = [line for line in text.splitlines() if long_path in line]
    assert matching, "expected long URL to appear on one line"
    assert long_url in matching[0]


def test_serialize_starts_and_ends_with_fences() -> None:
    """Output starts with ``---\\n`` and ends with ``---\\n``."""
    text = serialize_frontmatter(_frontmatter())
    assert text.startswith("---\n")
    assert text.endswith("---\n")


def test_serialize_omits_empty_string_fields() -> None:
    """Empty-string values are treated as missing — no ``author: ''`` leaks."""
    fm = _frontmatter(author="", description="", site_name="", language="")
    yaml_text = serialize_frontmatter(fm)
    assert "author:" not in yaml_text
    assert "description:" not in yaml_text
    assert "site_name:" not in yaml_text
    assert "language:" not in yaml_text
    # Sanity: the required string fields (url / final_url) stay even when
    # they are non-empty — the rule is "drop empties", not "drop strings".
    assert "url: https://example.com/post" in yaml_text


def test_serialize_keeps_whitespace_only_fields() -> None:
    """Whitespace-only strings are kept — only ``None`` and ``""`` are dropped."""
    fm = _frontmatter(title="   ")
    text = serialize_frontmatter(fm)
    assert "title:" in text


def test_serialize_description_with_newlines_roundtrips() -> None:
    """Multi-line ``description`` survives YAML round-trip byte-for-byte."""
    import yaml

    multi = "Line one.\nLine two.\nLine three."
    fm = _frontmatter(description=multi)
    text = serialize_frontmatter(fm)
    # Strip the fences before parsing back.
    inner = text.removeprefix("---\n").removesuffix("---\n")
    parsed = yaml.safe_load(inner)
    assert parsed["description"] == multi


def test_slugify_uses_title_when_present() -> None:
    """Title takes precedence over URL."""
    path = slugify_default_path(
        make_fetched_doc(url="https://example.com/foo"),
        _make_extracted(title="Why FastAPI?"),
    )
    assert path == Path("why-fastapi.md")


def test_slugify_falls_back_to_url_path_when_title_empty() -> None:
    """Empty title → last path segment of the URL."""
    path = slugify_default_path(
        make_fetched_doc(url="https://example.com/blog/why-fastapi"),
        _make_extracted(title=""),
    )
    assert path == Path("why-fastapi.md")


def test_slugify_handles_trailing_slash() -> None:
    """Trailing slash → uses the last non-empty segment."""
    path = slugify_default_path(
        make_fetched_doc(url="https://example.com/blog/why-fastapi/"),
        _make_extracted(title=None),
    )
    assert path == Path("why-fastapi.md")


def test_slugify_uses_host_when_only_root() -> None:
    """Bare host URL → host-based slug."""
    path = slugify_default_path(
        make_fetched_doc(url="https://example.com/"),
        _make_extracted(title=None),
    )
    assert path == Path("example-com.md")


def test_slugify_unicode_garbage_falls_back_to_page() -> None:
    """All-emoji title with no useful URL → ``"page.md"``."""
    path = slugify_default_path(
        make_fetched_doc(url="https://example.com/"),
        # Title is all emoji; the URL path is empty so the host saves us
        # from a "page.md" fallback unless we also wipe the host. Use a URL
        # whose host also collapses to nothing under slugify.
        _make_extracted(title="🚀🚀🚀"),
    )
    # Title slugs to "" but host "example.com" yields a valid slug.
    assert path == Path("example-com.md")

    # Now the true fallback: empty path AND empty/garbage host.
    fallback = slugify_default_path(
        FetchedDoc(
            url="🚀",
            final_url="🚀",
            status_code=200,
            html="",
            content_type=None,
            encoding=None,
            headers={},
        ),
        _make_extracted(title="🚀🚀🚀"),
    )
    assert fallback == Path("page.md")


def test_slugify_caps_length_at_80() -> None:
    """Long titles slug to ≤80 chars (excluding ``.md`` suffix)."""
    long_title = "Why " + ("FastAPI " * 30)
    path = slugify_default_path(
        make_fetched_doc(url="https://example.com/blog/why-fastapi"),
        _make_extracted(title=long_title),
    )
    slug = path.stem  # strip ".md"
    assert len(slug) <= 80


def test_slugify_is_pure() -> None:
    """Two identical calls return equal paths (no hidden state)."""
    fetched = make_fetched_doc(url="https://example.com/blog/why-fastapi")
    extracted = _make_extracted(title="Stable Title")
    a = slugify_default_path(fetched, extracted)
    b = slugify_default_path(fetched, extracted)
    assert a == b


def test_slugify_does_not_suffix_non_reserved_prefix() -> None:
    """Only exact matches trigger the suffix — ``CONference`` is left alone."""
    path = slugify_default_path(
        make_fetched_doc(url="https://example.com/x"),
        _make_extracted(title="CONference"),
    )
    assert path == Path("conference.md")


def test_write_happy_path(tmp_path: Path) -> None:
    """Writes frontmatter + body + trailing newline; returns target."""
    target = tmp_path / "out.md"
    fm = _frontmatter()
    result = write_output("body line\n", fm, output=target, overwrite=False)

    assert result == target
    content = target.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "body line\n" in content
    assert content.endswith("body line\n")


@pytest.mark.parametrize(
    ("body", "expected_suffix"),
    [
        ("no trailing", "no trailing\n"),
        ("body\n\n\n\n", "body\n"),
    ],
    ids=["missing_newline", "excess_newlines"],
)
def test_write_normalises_trailing_newline(tmp_path: Path, body: str, expected_suffix: str) -> None:
    """write_output ensures the output file ends with exactly one trailing newline."""
    target = tmp_path / "out.md"
    write_output(body, _frontmatter(), output=target, overwrite=False)
    content = target.read_text(encoding="utf-8")
    assert content.endswith(expected_suffix)
    assert not content.endswith(expected_suffix + "\n")


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    """Missing parent dirs are created."""
    target = tmp_path / "nested" / "deep" / "out.md"
    result = write_output("body\n", _frontmatter(), output=target, overwrite=False)
    assert result == target
    assert target.exists()


def test_write_exists_no_overwrite_raises(tmp_path: Path) -> None:
    """Existing file + no overwrite → :class:`WriteError`."""
    target = tmp_path / "out.md"
    target.write_text("original content\n", encoding="utf-8")

    with pytest.raises(WriteError):
        write_output("body\n", _frontmatter(), output=target, overwrite=False)

    # File is untouched.
    assert target.read_text(encoding="utf-8") == "original content\n"


def test_write_exists_overwrite_replaces(tmp_path: Path) -> None:
    """Existing file + overwrite → writes; warning logged."""
    target = tmp_path / "out.md"
    target.write_text("OLD\n", encoding="utf-8")

    with capture_logs() as events:
        write_output("new body\n", _frontmatter(), output=target, overwrite=True)

    content = target.read_text(encoding="utf-8")
    assert "new body\n" in content
    assert content != "OLD\n"
    assert any(
        ev.get("event") == "write.overwrite" and ev.get("path") == str(target) for ev in events
    )


def test_atomic_write_failure_cleans_up_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure during write removes the temp file; raises ``WriteError``."""
    target = tmp_path / "out.md"
    target.write_text("PRESERVE\n", encoding="utf-8")

    def _boom(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("pagetomd.writer.os.fsync", _boom)

    with pytest.raises(WriteError):
        write_output("body\n", _frontmatter(), output=target, overwrite=True)

    # Original file untouched.
    assert target.read_text(encoding="utf-8") == "PRESERVE\n"
    # No leaked temp files in the directory.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".out.md.tmp.")]
    assert leftovers == []


def test_permission_error_wrapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PermissionError`` → ``WriteError`` carrying the original message."""
    target = tmp_path / "out.md"

    real_open = open

    def _denied(*args: Any, **kwargs: Any) -> Any:
        # Only intercept the temp-file open inside _atomic_write; everything
        # else uses the real ``open``.
        if args and isinstance(args[0], (str, os.PathLike)):
            name = os.fspath(args[0])
            if ".out.md.tmp." in name:
                raise PermissionError("permission denied")
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", _denied)

    with pytest.raises(WriteError) as exc_info:
        write_output("body\n", _frontmatter(), output=target, overwrite=False)

    assert "permission denied" in exc_info.value.message.lower()


def test_no_utf8_bom_written(tmp_path: Path) -> None:
    """File is plain UTF-8 (no ``\\ufeff`` BOM)."""
    target = tmp_path / "out.md"
    write_output("body\n", _frontmatter(title="café"), output=target, overwrite=False)
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("body", ["body line one\nbody line two\n", "single\n"])
def test_newline_mode_is_lf_only(tmp_path: Path, body: str) -> None:
    """File contains only ``\\n`` line endings (no ``\\r\\n``)."""
    target = tmp_path / "out.md"
    write_output(body, _frontmatter(), output=target, overwrite=False)
    raw = target.read_bytes()
    assert b"\r\n" not in raw
    assert b"\r" not in raw


def test_stdout_path_dash_writes_to_stdout(
    chdir_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``Path('-')`` writes to stdout, returns ``None``, no file created."""
    result = write_output(
        "body line\n",
        _frontmatter(),
        output=Path("-"),
        overwrite=False,
    )
    assert result is None

    captured = capsys.readouterr()
    assert captured.out.startswith("---\n")
    assert "body line\n" in captured.out
    # Frontmatter must precede body.
    fm_end = captured.out.index("---\n", 4)  # 2nd '---' marker
    body_pos = captured.out.index("body line")
    assert fm_end < body_pos
    # No file accidentally created in CWD.
    assert list(chdir_tmp.iterdir()) == []


def test_stdout_ignores_overwrite_flag(
    chdir_tmp: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stdout mode does not check ``overwrite``."""
    # Even with overwrite=False, stdout writes succeed (and stdout is not a
    # file, so the "exists" check must never trigger).
    result = write_output(
        "body\n",
        _frontmatter(),
        output=Path("-"),
        overwrite=False,
    )
    assert result is None
    assert capsys.readouterr().out  # produced something


def test_write_ok_event_fires(tmp_path: Path) -> None:
    """``write.ok`` event is emitted with the ``path`` field."""
    target = tmp_path / "out.md"
    with capture_logs() as events:
        write_output("body\n", _frontmatter(), output=target, overwrite=False)
    ok_events = [ev for ev in events if ev.get("event") == "write.ok"]
    assert ok_events, "expected a write.ok event"
    assert ok_events[0].get("path") == str(target)


def test_write_output_none_raises_write_error() -> None:
    """Calling with ``output=None`` (CLI bug) raises a typed ``WriteError``."""
    with pytest.raises(WriteError) as exc_info:
        write_output("body\n", _frontmatter(), output=None, overwrite=False)
    assert "not provided" in exc_info.value.message.lower()


def test_ensure_parent_dir_failure_wrapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mkdir`` failure is wrapped in ``WriteError`` with ``path`` set."""
    target = tmp_path / "nested" / "out.md"

    def _boom(self: Path, *_a: Any, **_k: Any) -> None:
        raise OSError("mkdir denied")

    monkeypatch.setattr(Path, "mkdir", _boom)

    with pytest.raises(WriteError) as exc_info:
        write_output("body\n", _frontmatter(), output=target, overwrite=False)
    assert "mkdir denied" in exc_info.value.message.lower()


_skip_if_no_symlinks = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="os.symlink requires admin/Developer Mode on Windows",
)


@_skip_if_no_symlinks
@pytest.mark.parametrize(
    ("overwrite", "expected_msg_fragment"),
    [
        (True, None),  # any WriteError is acceptable
        (False, "symlink"),
    ],
    ids=["overwrite_true", "overwrite_false"],
)
def test_symlink_target_refused_follow_symlinks_false(
    tmp_path: Path, overwrite: bool, expected_msg_fragment: str | None
) -> None:
    """Writing to a symlink with follow_symlinks=False is refused."""
    real = tmp_path / "real.md"
    real.write_text("ORIGINAL CONTENT\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    with pytest.raises(WriteError) as exc_info:
        write_output(
            "new body\n",
            _frontmatter(),
            output=link,
            overwrite=overwrite,
            follow_symlinks=False,
        )

    if expected_msg_fragment:
        assert expected_msg_fragment in exc_info.value.message.lower()
    assert real.read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"


@_skip_if_no_symlinks
def test_symlink_target_followed_when_opted_in(tmp_path: Path) -> None:
    """``follow_symlinks=True`` + ``overwrite=True`` → link target replaced, link preserved."""
    real = tmp_path / "real.md"
    real.write_text("ORIGINAL CONTENT\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)

    result = write_output(
        "new body\n",
        _frontmatter(),
        output=link,
        overwrite=True,
        follow_symlinks=True,
    )

    assert result == link
    # New document must have landed at the link's target.
    assert "new body" in real.read_text(encoding="utf-8")
    # The link should still be a link (we didn't blow it away with a regular
    # file write that bypassed the link).
    assert link.is_symlink()


def test_regular_file_overwrite_unaffected_by_symlink_guard(tmp_path: Path) -> None:
    """Regular file + ``overwrite=True`` → symlink guard is a no-op; normal overwrite proceeds."""
    target = tmp_path / "out.md"
    target.write_text("ORIGINAL CONTENT\n", encoding="utf-8")

    result = write_output(
        "new body\n",
        _frontmatter(),
        output=target,
        overwrite=True,
        follow_symlinks=False,
    )

    assert result == target
    assert "new body" in target.read_text(encoding="utf-8")


def test_nonexistent_target_succeeds_regardless_of_follow_symlinks(
    tmp_path: Path,
) -> None:
    """Non-existent target writes succeed for both ``follow_symlinks`` values."""
    for flag in (False, True):
        target = tmp_path / f"fresh-{flag}.md"
        result = write_output(
            "body\n",
            _frontmatter(),
            output=target,
            overwrite=False,
            follow_symlinks=flag,
        )
        assert result == target
        assert target.is_file()
