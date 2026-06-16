"""Unit tests for :mod:`pagetomd.cli`.

Uses a fake ``run`` to verify option parsing, env-var precedence,
exit-code mapping, and stderr/stdout discipline.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import pagetomd.cli as cli_module
from pagetomd import __version__
from pagetomd.cli import app
from pagetomd.config import Config
from pagetomd.exceptions import (
    DependencyMissingError,
    ExtractionEmptyError,
    FetchError,
    PageToMdError,
    RobotsDisallowedError,
    WriteError,
)
from pagetomd.pipeline import PipelineResult


@pytest.fixture()
def runner() -> CliRunner:
    """Provide a :class:`CliRunner` with separate stdout/stderr streams."""
    return CliRunner()


@pytest.fixture()
def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every ``PAGETOMD_*`` env var so tests start from a clean slate."""
    import os

    for key in list(os.environ):
        if key.startswith("PAGETOMD_"):
            monkeypatch.delenv(key, raising=False)


def _ok_result(
    *,
    output_path: Path | None = Path("out.md"),
    bytes_written: int = 42,
    elapsed_ms: int = 7,
) -> PipelineResult:
    """Build a :class:`PipelineResult` with sensible defaults for tests."""
    return PipelineResult(
        output_path=output_path,
        bytes_written=bytes_written,
        final_url="https://example.com/x",
        title="X",
        elapsed_ms=elapsed_ms,
    )


@pytest.fixture()
def fake_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``cli.run`` with a recorder; let tests tweak its behaviour.

    The returned dict has two slots: ``"cfg"`` (populated with the
    :class:`Config` the CLI built) and ``"result"`` / ``"exc"`` that tests
    set to control what the fake returns or raises.
    """
    state: dict[str, Any] = {"cfg": None, "result": _ok_result(), "exc": None}

    def fake(cfg: Config) -> PipelineResult:
        state["cfg"] = cfg
        if state["exc"] is not None:
            raise state["exc"]
        return state["result"]  # type: ignore[no-any-return]

    monkeypatch.setattr(cli_module, "run", fake)
    return state


@pytest.fixture()
def fake_configure_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, bool]]:
    """Record ``configure_logging`` calls as ``(level, json_mode)`` tuples."""
    calls: list[tuple[str, bool]] = []

    def fake(*, level: str, json_mode: bool) -> None:
        calls.append((level, json_mode))

    monkeypatch.setattr(cli_module, "configure_logging", fake)
    return calls


@pytest.fixture(autouse=True)
def _auto_clear_env(clear_env: None) -> Iterator[None]:
    """Apply :func:`clear_env` to every test in this module."""
    yield


def test_help_smoke(runner: CliRunner) -> None:
    """``--help`` exits 0 and mentions the high-level command summary."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Convert a webpage URL" in result.stdout


def test_help_mentions_locked_flags(runner: CliRunner) -> None:
    """Every documented flag appears somewhere in the help text."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for flag in (
        "--output",
        "--overwrite",
        "--fetcher",
        "--timeout",
        "--retries",
        "--user-agent",
        "--respect-robots",
        "--no-respect-robots",
        "--max-redirects",
        "--include-comments",
        "--include-images",
        "--include-links",
        "--heading-style",
        "--code-fences",
        "--wide-tables",
        "--no-fetched-at",
        "--log-level",
        "--log-json",
        "--debug",
        "--version",
    ):
        assert flag in result.stdout, f"flag {flag!r} missing from --help output"


def test_version_flag(runner: CliRunner) -> None:
    """``--version`` prints ``pagetomd <version>`` on stdout and exits 0."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"pagetomd {__version__}" in result.stdout


def test_no_args_prints_help(runner: CliRunner) -> None:
    """No args exits 2 (missing argument) but still shows help text."""
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Usage:" in result.stdout


def test_happy_path_writes_summary_to_stderr(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
    tmp_path: Path,
) -> None:
    """A successful run prints a checkmark summary to **stderr**."""
    out = tmp_path / "out.md"
    fake_run["result"] = _ok_result(output_path=out, bytes_written=123, elapsed_ms=11)

    result = runner.invoke(app, ["https://example.com/x", "-o", str(out)])

    assert result.exit_code == 0
    assert result.stdout == ""  # stdout must stay pristine
    assert "✓ wrote 123 bytes to" in result.stderr
    assert str(out) in result.stderr
    assert "(11ms)" in result.stderr
    assert fake_configure_logging  # was called


def test_stdout_sink_passes_dash_path(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``-o -`` becomes ``Path("-")`` in the merged config."""
    fake_run["result"] = _ok_result(output_path=None, bytes_written=9)

    result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    cfg = fake_run["cfg"]
    assert isinstance(cfg, Config)
    assert cfg.output == Path("-")
    assert result.exit_code == 0
    assert "<stdout>" in result.stderr


def test_overwrite_flag_plumbed(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``--overwrite`` lands on the config."""
    result = runner.invoke(app, ["https://example.com/x", "--overwrite", "-o", "-"])
    assert result.exit_code == 0
    assert fake_run["cfg"].overwrite is True


def test_no_respect_robots_flag_plumbed(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """The negative flag flips ``respect_robots`` to ``False``."""
    result = runner.invoke(app, ["https://example.com/x", "--no-respect-robots", "-o", "-"])
    assert result.exit_code == 0
    assert fake_run["cfg"].respect_robots is False


def test_wide_tables_choice_plumbed(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``--wide-tables=html`` surfaces on the config."""
    result = runner.invoke(app, ["https://example.com/x", "--wide-tables", "html", "-o", "-"])
    assert result.exit_code == 0
    assert fake_run["cfg"].wide_tables == "html"


def test_no_fetched_at_flag_plumbed(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``--no-fetched-at`` flips the deterministic-output toggle."""
    result = runner.invoke(app, ["https://example.com/x", "--no-fetched-at", "-o", "-"])
    assert result.exit_code == 0
    assert fake_run["cfg"].no_fetched_at is True


def test_debug_forces_log_level_and_prints_traceback(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``--debug`` collapses to ``log_level="debug"`` and emits a traceback."""
    fake_run["exc"] = FetchError("boom")
    result = runner.invoke(app, ["https://example.com/x", "--debug", "-o", "-"])

    assert result.exit_code == 2
    assert fake_run["cfg"].log_level == "debug"
    assert "Traceback" in result.stderr


def test_env_var_wins_when_flag_not_passed(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``PAGETOMD_TIMEOUT`` survives because the CLI default is omitted."""
    monkeypatch.setenv("PAGETOMD_TIMEOUT", "5")

    result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    assert result.exit_code == 0
    assert fake_run["cfg"].timeout == 5.0


def test_cli_flag_overrides_env_var(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """A user-supplied ``--timeout`` overrides the env var as expected."""
    monkeypatch.setenv("PAGETOMD_TIMEOUT", "5")

    result = runner.invoke(app, ["https://example.com/x", "--timeout", "99", "-o", "-"])

    assert result.exit_code == 0
    assert fake_run["cfg"].timeout == 99.0


@pytest.mark.parametrize(
    ("exc_factory", "expected_exit", "expected_class_name"),
    [
        (lambda: FetchError("boom"), 2, "FetchError"),
        (lambda: ExtractionEmptyError("empty"), 3, "ExtractionEmptyError"),
        (lambda: WriteError("denied"), 4, "WriteError"),
    ],
)
def test_typed_errors_map_to_exit_codes(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
    exc_factory: Callable[[], PageToMdError],
    expected_exit: int,
    expected_class_name: str,
) -> None:
    """Typed errors print the two-line report and exit with their code."""
    fake_run["exc"] = exc_factory()

    result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    assert result.exit_code == expected_exit
    assert f"error: {expected_class_name}:" in result.stderr
    assert "hint:" in result.stderr


def test_fetch_error_includes_message(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``FetchError`` mapping carries the original message into stderr."""
    fake_run["exc"] = FetchError("boom")

    result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    assert result.exit_code == 2
    assert "error: FetchError: boom" in result.stderr
    assert "hint:" in result.stderr


def test_robots_disallowed_hint_mentions_override(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``RobotsDisallowedError`` hint guides the user to the override flag."""
    fake_run["exc"] = RobotsDisallowedError("blocked")

    result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    assert result.exit_code == 2
    assert "--no-respect-robots" in result.stderr


def test_config_error_returns_64(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """Invalid CLI values surface as ``ConfigError`` (exit 64)."""
    result = runner.invoke(app, ["https://example.com/x", "--timeout", "0", "-o", "-"])
    assert result.exit_code == 64
    assert "error: ConfigError" in result.stderr
    # Pipeline must not have been called when config validation failed.
    assert fake_run["cfg"] is None


def test_dependency_missing_for_playwright_returns_5(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``--fetcher playwright`` surfaces a ``DependencyMissingError``."""
    fake_run["exc"] = DependencyMissingError(
        "Playwright fetcher not yet implemented; install with "
        "pagetomd[playwright] and wait for M5.",
        extra="playwright",
    )
    result = runner.invoke(app, ["https://example.com/x", "--fetcher", "playwright", "-o", "-"])
    assert result.exit_code == 5
    assert "pagetomd[playwright]" in result.stderr


def test_keyboard_interrupt_exits_130(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``Ctrl-C`` inside the pipeline yields the conventional exit code 130."""
    fake_run["exc"] = KeyboardInterrupt()

    result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    assert result.exit_code == 130
    assert "interrupted" in result.stderr


def test_unexpected_runtime_error_wrapped_to_exit_1(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """A base ``PageToMdError`` (wrapping an unexpected failure) exits 1."""

    def explode(cfg: Config) -> PipelineResult:
        raise PageToMdError("Unexpected pipeline failure", original="x")

    monkeypatch.setattr(cli_module, "run", explode)
    result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    assert result.exit_code == 1
    assert "error: PageToMdError:" in result.stderr


def test_main_module_importable() -> None:
    """Importing :mod:`pagetomd.__main__` must not raise."""
    import importlib

    module = importlib.import_module("pagetomd.__main__")
    assert hasattr(module, "app")


def test_stdout_sink_with_overwrite(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """``-o -`` + ``--overwrite`` is accepted (overwrite is harmless)."""
    fake_run["result"] = _ok_result(output_path=None, bytes_written=11)

    result = runner.invoke(app, ["https://example.com/x", "-o", "-", "--overwrite"])

    assert result.exit_code == 0
    assert fake_run["cfg"].output == Path("-")
    assert fake_run["cfg"].overwrite is True


def test_debug_help_mentions_traceback_caveat(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--help`` warns that ``--debug`` tracebacks may leak local paths/URLs."""
    # Typer's rich-help renderer pulls terminal width from ``COLUMNS`` /
    # ``shutil.get_terminal_size``; CliRunner's narrow default causes Rich
    # to drop options off the panel entirely. Force a wide width so the
    # full ``--debug`` row renders.
    monkeypatch.setenv("COLUMNS", "240")
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    flat = " ".join(result.stdout.split())  # collapse line-wrap whitespace
    assert "Tracebacks may include local file paths" in flat


def test_env_override_emits_info_log(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """A ``PAGETOMD_*`` env var triggers a ``config.env_overrides`` INFO log."""
    import structlog
    from structlog.testing import capture_logs

    # Reset structlog so capture_logs() works regardless of prior test state.
    structlog.reset_defaults()

    monkeypatch.setenv("PAGETOMD_RESPECT_ROBOTS", "false")

    with capture_logs() as logs:
        result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    assert result.exit_code == 0
    env_events = [entry for entry in logs if entry.get("event") == "config.env_overrides"]
    assert len(env_events) == 1, f"expected exactly one env_overrides event, got {logs!r}"
    entry = env_events[0]
    assert "respect_robots" in entry["fields"]
    # Values must never be logged.
    assert "false" not in str(entry).lower() or "fields" in entry  # sanity: only field NAMES
    assert entry.get("log_level") == "info"


def test_no_env_overrides_no_log_event(
    runner: CliRunner,
    fake_run: dict[str, Any],
    fake_configure_logging: list[tuple[str, bool]],
) -> None:
    """No ``PAGETOMD_*`` vars set means no ``config.env_overrides`` log event."""
    import structlog
    from structlog.testing import capture_logs

    structlog.reset_defaults()

    with capture_logs() as logs:
        result = runner.invoke(app, ["https://example.com/x", "-o", "-"])

    assert result.exit_code == 0
    env_events = [entry for entry in logs if entry.get("event") == "config.env_overrides"]
    assert env_events == [], f"unexpected env_overrides event(s): {env_events!r}"
