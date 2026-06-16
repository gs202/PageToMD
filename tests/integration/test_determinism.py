"""Determinism guarantees for the end-to-end CLI pipeline.

Proves byte-identical output across consecutive runs (with and without ``--no-fetched-at``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import RunCli

pytestmark = pytest.mark.integration


# Fixtures the determinism contract must hold on. Picked to span prose
# (``blog``), heavily-headed docs (``docs``), and right-to-left content
# (``rtl_hebrew``); together they exercise the heading normaliser, the
# code-block fencer, and the unicode-normalisation paths.
_DETERMINISTIC_FIXTURES: tuple[str, ...] = ("blog.html", "docs.html", "rtl_hebrew.html")


@pytest.mark.parametrize("fixture", _DETERMINISTIC_FIXTURES)
def test_no_fetched_at_produces_byte_identical_runs(
    fixture: str,
    run_cli: RunCli,
    local_http_server: str,
    tmp_path: Path,
) -> None:
    """Two runs with ``--no-fetched-at`` write byte-equal Markdown files."""
    url = f"{local_http_server}/{fixture}"
    out1 = tmp_path / "run_1.md"
    out2 = tmp_path / "run_2.md"

    for out in (out1, out2):
        # ``expected_exit=None`` so we own the assertion and can emit a
        # fixture-aware diagnostic on failure (the helper's generic
        # assert message would not name which fixture failed).
        result = run_cli(
            [
                url,
                "-o",
                str(out),
                "--no-fetched-at",
                "--no-respect-robots",
                "--log-level=error",
            ],
            expected_exit=None,
        )
        assert result.returncode == 0, (
            f"pagetomd exited {result.returncode} for {fixture}: stderr={result.stderr!r}"
        )

    bytes1 = out1.read_bytes()
    bytes2 = out2.read_bytes()
    assert bytes1 == bytes2, (
        f"determinism violated for {fixture}: "
        f"len(run_1)={len(bytes1)} len(run_2)={len(bytes2)} "
        f"first-diff-byte={_first_diff(bytes1, bytes2)}"
    )


@pytest.mark.parametrize("fixture", _DETERMINISTIC_FIXTURES)
def test_fetched_at_is_the_only_per_run_variation(
    fixture: str,
    run_cli: RunCli,
    local_http_server: str,
    tmp_path: Path,
) -> None:
    """Without ``--no-fetched-at``, only the ``fetched_at:`` line differs."""
    url = f"{local_http_server}/{fixture}"
    out1 = tmp_path / "run_1.md"
    out2 = tmp_path / "run_2.md"

    for out in (out1, out2):
        # ``expected_exit=None`` so we own the assertion and can emit a
        # fixture-aware diagnostic on failure (the helper's generic
        # assert message would not name which fixture failed).
        result = run_cli(
            [
                url,
                "-o",
                str(out),
                "--no-respect-robots",
                "--log-level=error",
            ],
            expected_exit=None,
        )
        assert result.returncode == 0, (
            f"pagetomd exited {result.returncode} for {fixture}: stderr={result.stderr!r}"
        )

    text1 = out1.read_text(encoding="utf-8")
    text2 = out2.read_text(encoding="utf-8")
    # Both runs MUST emit a fetched_at line — this test is meaningless
    # otherwise. Catch the regression where the field stops appearing.
    assert "fetched_at:" in text1
    assert "fetched_at:" in text2

    stripped1 = _strip_fetched_at(text1)
    stripped2 = _strip_fetched_at(text2)
    assert stripped1 == stripped2, (
        f"per-run variation outside fetched_at for {fixture}: "
        f"first diff at line {_first_diff_line(stripped1, stripped2)}"
    )


def _strip_fetched_at(text: str) -> str:
    """Return ``text`` with any ``fetched_at:`` frontmatter line removed.

    Operates line by line so we can be exact about what disappears: only
    the single key-value pair line, not the surrounding YAML structure.
    """
    return "\n".join(line for line in text.split("\n") if not line.startswith("fetched_at:"))


def _first_diff(a: bytes, b: bytes) -> int:
    """Return the byte offset of the first difference, or ``-1`` if equal."""
    for idx, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return idx
    if len(a) != len(b):
        return min(len(a), len(b))
    return -1


def _first_diff_line(a: str, b: str) -> int:
    """Return the 1-based line number of the first difference, or ``-1``."""
    for idx, (x, y) in enumerate(zip(a.split("\n"), b.split("\n"), strict=False), start=1):
        if x != y:
            return idx
    return -1
