"""End-to-end CLI tests against the loopback HTTP server.

Exercises the full ``python -m pagetomd`` subprocess entrypoint with no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import RunCli

pytestmark = pytest.mark.integration


def test_blog_to_file(run_cli: RunCli, local_http_server: str, tmp_path: Path) -> None:
    """Convert ``blog.html`` to a real Markdown file."""
    out = tmp_path / "out.md"
    run_cli([f"{local_http_server}/blog.html", "-o", str(out)])
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "Why We Rewrote Our Build System in Rust" in text


def test_docs_to_stdout(run_cli: RunCli, local_http_server: str) -> None:
    """``-o -`` streams the rendered Markdown to stdout."""
    result = run_cli([f"{local_http_server}/docs.html", "-o", "-"])
    assert result.stdout.startswith("---")
    # Body content also present after the frontmatter.
    assert "Configuration Reference" in result.stdout


def test_sequential_conversions(run_cli: RunCli, local_http_server: str, tmp_path: Path) -> None:
    """Multiple fixtures all succeed independently."""
    for name in ("docs.html", "news.html", "rtl_hebrew.html"):
        out = tmp_path / f"{name}.md"
        run_cli([f"{local_http_server}/{name}", "-o", str(out)])
        assert out.exists()
        # Each output is well-formed (starts with frontmatter).
        assert out.read_text(encoding="utf-8").startswith("---")


def test_no_fetched_at_omits_field(run_cli: RunCli, local_http_server: str, tmp_path: Path) -> None:
    """``--no-fetched-at`` produces frontmatter without ``fetched_at:``."""
    out = tmp_path / "out.md"
    run_cli([f"{local_http_server}/blog.html", "-o", str(out), "--no-fetched-at"])
    assert "fetched_at:" not in out.read_text(encoding="utf-8")


def test_overwrite_replaces_existing_file(
    run_cli: RunCli, local_http_server: str, tmp_path: Path
) -> None:
    """``--overwrite`` writes over an existing destination without error."""
    out = tmp_path / "out.md"
    out.write_text("placeholder", encoding="utf-8")
    run_cli([f"{local_http_server}/blog.html", "-o", str(out), "--overwrite"])
    text = out.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "placeholder" not in text


def test_existing_target_without_overwrite_fails(
    run_cli: RunCli, local_http_server: str, tmp_path: Path
) -> None:
    """No ``--overwrite`` against an existing file → ``WriteError`` exit 4."""
    out = tmp_path / "out.md"
    out.write_text("existing content", encoding="utf-8")
    result = run_cli(
        [f"{local_http_server}/blog.html", "-o", str(out)],
        expected_exit=4,
    )
    assert "WriteError" in result.stderr


def test_robots_missing_on_localhost_proceeds(
    run_cli: RunCli, local_http_server: str, tmp_path: Path
) -> None:
    """Missing ``/robots.txt`` (404) is treated as unrestricted."""
    out = tmp_path / "out.md"
    # ``--respect-robots`` is the default; the fixture server returns 404
    # for ``/robots.txt`` and the fetcher must still proceed.
    run_cli([f"{local_http_server}/docs.html", "-o", str(out)])
    assert out.exists()


def test_wide_table_modes_produce_distinct_and_correct_output(
    run_cli: RunCli, local_http_server: str, tmp_path: Path
) -> None:
    """The three wide-table modes produce correct and distinct outputs end-to-end."""
    outputs: dict[str, str] = {}
    for mode in ("kv", "html", "drop"):
        out = tmp_path / f"tables_{mode}.md"
        run_cli([f"{local_http_server}/tables.html", "-o", str(out), "--wide-tables", mode])
        outputs[mode] = out.read_text(encoding="utf-8")

    # Mode-specific content checks
    assert "### Row 1" in outputs["kv"] and "- **Date**" in outputs["kv"]
    assert "<table" in outputs["html"] and "<row" not in outputs["html"]
    assert "pagetomd: wide table dropped" in outputs["drop"]

    # Cross-mode inequality (ensures they are distinct)
    assert outputs["kv"] != outputs["html"]
    assert outputs["html"] != outputs["drop"]
    assert outputs["kv"] != outputs["drop"]


def test_invalid_url_fetch_error(run_cli: RunCli) -> None:
    """A non-URL argument fails fast with the ``FetchError`` exit code."""
    result = run_cli(["not-a-url"], expected_exit=2)
    assert "FetchError" in result.stderr or "error" in result.stderr.lower()
