"""End-to-end Playwright fetcher integration smoke test."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.playwright]


@pytest.mark.playwright
def test_cli_playwright_smoke_to_stdout(
    chromium_available: bool,
    local_http_server: str,
) -> None:
    """``pagetomd <url> --fetcher playwright -o -`` exits 0 with content."""
    if not chromium_available:
        pytest.skip("chromium not available; run `playwright install chromium`")

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    cmd = [
        sys.executable,
        "-m",
        "pagetomd",
        f"{local_http_server}/spa_vue.html",
        "--fetcher",
        "playwright",
        "-o",
        "-",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=60.0,
        check=False,
    )
    assert result.returncode == 0, (
        f"exit={result.returncode} stderr={result.stderr!r} stdout={result.stdout[:500]!r}"
    )
    assert result.stdout.startswith("---")
    # Hydration-rendered article body is visible in the converted Markdown.
    assert "Understanding Reactive State" in result.stdout
