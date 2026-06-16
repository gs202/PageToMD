"""Snapshot-test-scoped configuration for tests/snapshot/."""

from __future__ import annotations

import pathlib

import pytest

_SNAPSHOT_DIR = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Apply the ``snapshot`` marker to every test collected under tests/snapshot/."""
    mark = pytest.mark.snapshot
    for item in items:
        if pathlib.Path(item.fspath).is_relative_to(_SNAPSHOT_DIR):
            item.add_marker(mark)
