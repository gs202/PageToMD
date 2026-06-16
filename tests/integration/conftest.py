"""Shared fixtures for the integration test package."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable

import pytest

# Public type alias for the fixture's return value. Tests that want to
# annotate the parameter explicitly (rather than relying on ``Callable``
# inference) can import this name from the conftest.
type RunCli = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture
def run_cli() -> RunCli:
    """Spawn ``python -m pagetomd`` as a subprocess.

    Accepts ``args``, optional ``expected_exit`` (default 0, ``None`` to skip),
    and ``timeout`` (default 30s). Returns ``CompletedProcess[str]``.
    """

    def _run(
        args: list[str],
        *,
        expected_exit: int | None = 0,
        timeout: float = 30.0,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, "-m", "pagetomd", *args]
        # Force unbuffered I/O so the captured streams are deterministic
        # across platforms (Windows pytest workers in particular).
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
        if expected_exit is not None:
            assert result.returncode == expected_exit, (
                f"exit={result.returncode} stderr={result.stderr!r} stdout={result.stdout[:500]!r}"
            )
        return result

    return _run
