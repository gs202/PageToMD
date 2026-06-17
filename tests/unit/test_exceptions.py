"""Unit tests for :mod:`pagetomd.exceptions`."""

from __future__ import annotations

import pytest

from pagetomd.exceptions import (
    ConfigError,
    ConversionError,
    DependencyMissingError,
    ExtractionEmptyError,
    FetchError,
    PageToMdError,
    RobotsDisallowedError,
    UsageError,
    WriteError,
)

# (exception class, expected exit code)
_EXPECTED_EXIT_CODES: list[tuple[type[PageToMdError], int]] = [
    (PageToMdError, 1),
    (UsageError, 64),
    (FetchError, 2),
    (RobotsDisallowedError, 2),
    (ExtractionEmptyError, 3),
    (ConversionError, 3),
    (WriteError, 4),
    (ConfigError, 64),
    (DependencyMissingError, 5),
]


@pytest.mark.parametrize(("exc_cls", "expected_code"), _EXPECTED_EXIT_CODES)
def test_exit_code_and_hint(exc_cls: type[PageToMdError], expected_code: int) -> None:
    """Each subclass must expose the documented exit code and a non-empty hint."""
    assert exc_cls.exit_code == expected_code
    assert isinstance(exc_cls.hint, str)
    assert exc_cls.hint.strip() != ""


def test_robots_disallowed_is_caught_as_fetch_error() -> None:
    """RobotsDisallowedError is caught by a FetchError handler (exit-code contract)."""
    err = RobotsDisallowedError("blocked")
    assert err.exit_code == 2
    assert "--no-respect-robots" in err.hint


@pytest.mark.parametrize("exc_cls", [cls for cls, _ in _EXPECTED_EXIT_CODES])
def test_str_includes_class_name_and_message(exc_cls: type[PageToMdError]) -> None:
    """__str__ must yield "ClassName: message" for every subclass."""
    err = exc_cls("boom")
    rendered = str(err)
    assert exc_cls.__name__ in rendered
    assert "boom" in rendered
    assert rendered == f"{exc_cls.__name__}: boom"


def test_message_attribute_is_exposed() -> None:
    """The raw message string is accessible as .message."""
    err = WriteError("disk full")
    assert err.message == "disk full"
