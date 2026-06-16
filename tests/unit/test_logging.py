"""Unit tests for :mod:`pagetomd.logging`."""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from pagetomd.logging import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_structlog() -> None:
    """Reset structlog state between tests so configure_logging is honoured."""
    structlog.reset_defaults()


def test_log_goes_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Records must be emitted on stderr; stdout stays empty for Markdown output."""
    configure_logging("debug")
    get_logger("x").info("hello", k="v")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello" in captured.err
    assert "k" in captured.err and "v" in captured.err


def test_json_mode_emits_valid_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    """In JSON mode each emitted line must be parseable JSON with expected keys."""
    configure_logging("debug", json_mode=True)
    get_logger("x").info("hello", k="v", number=3)

    captured = capsys.readouterr()
    assert captured.out == ""
    lines = [ln for ln in captured.err.splitlines() if ln.strip()]
    assert lines, "expected at least one JSON log line on stderr"

    payload = json.loads(lines[-1])
    assert payload["event"] == "hello"
    assert payload["k"] == "v"
    assert payload["number"] == 3
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_filtering_suppresses_debug_when_level_is_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A debug-level record is dropped when configured level is warning."""
    configure_logging("warning", json_mode=True)
    log = get_logger("x")
    log.debug("invisible", scope="quiet")
    log.warning("visible", scope="loud")

    captured = capsys.readouterr()
    assert "invisible" not in captured.err
    assert "visible" in captured.err


from structlog.testing import capture_logs

def test_get_logger_returns_bound_logger() -> None:
    """get_logger returns a structlog BoundLogger that carries bound context."""
    configure_logging("info")
    log = get_logger("named")
    bound = log.bind(request_id="abc")
    with capture_logs() as cap:
        bound.info("event")
    assert cap[0]["request_id"] == "abc"


def test_configure_logging_sets_root_level() -> None:
    """The stdlib root logger level reflects the requested string level."""
    configure_logging("error")
    assert logging.getLogger().level == logging.ERROR

    configure_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG


def test_unknown_level_falls_back_to_info() -> None:
    """An unrecognised level string must not raise; it falls back to INFO."""
    configure_logging("not-a-real-level")
    assert logging.getLogger().level == logging.INFO
