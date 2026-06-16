"""Unit-test-scoped fixtures shared across all tests/unit/ modules."""

from __future__ import annotations

import os
import pathlib

import pytest

_UNIT_DIR = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the ``unit`` marker to every test collected under tests/unit/."""
    mark = pytest.mark.unit
    for item in items:
        if pathlib.Path(item.fspath).is_relative_to(_UNIT_DIR):
            item.add_marker(mark)


@pytest.fixture(autouse=True)
def _scrub_pagetomd_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ``PAGETOMD_*`` env var before each unit test.

    Prevents environment variables set in one test from leaking into the
    next. Applied automatically to every test in the ``tests/unit/``
    package.
    """
    for key in list(os.environ):
        if key.startswith("PAGETOMD_") and key != "PAGETOMD_INTERNAL_SKIP_SSRF":
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit tenacity sleeps so retry tests stay fast."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _seconds: None)
