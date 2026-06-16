"""Unit tests for :mod:`pagetomd.config`."""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from pagetomd.config import Config
from pagetomd.exceptions import ConfigError


def test_defaults_applied_when_only_url_supplied() -> None:
    """Supplying only ``url`` should populate every other field from the defaults."""
    cfg = Config.from_overrides({"url": "https://example.com"})

    assert cfg.url == "https://example.com"
    assert cfg.output is None
    assert cfg.overwrite is False
    assert cfg.fetcher == "httpx"
    assert cfg.timeout == 30.0
    assert cfg.retries == 3
    assert cfg.user_agent.startswith("pagetomd/")
    assert cfg.respect_robots is True
    assert cfg.follow_redirects is True
    assert cfg.max_redirects == 10
    assert cfg.include_comments is False
    assert cfg.include_images is True
    assert cfg.include_links is True
    assert cfg.heading_style == "atx"
    assert cfg.code_fences is True
    assert cfg.wide_tables == "kv"
    assert cfg.no_fetched_at is False
    assert cfg.log_level == "info"
    assert cfg.log_json is False


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables prefixed with PAGETOMD_ should override defaults."""
    monkeypatch.setenv("PAGETOMD_TIMEOUT", "5")
    monkeypatch.setenv("PAGETOMD_RETRIES", "7")
    monkeypatch.setenv("PAGETOMD_FETCHER", "playwright")
    monkeypatch.setenv("PAGETOMD_RESPECT_ROBOTS", "false")
    monkeypatch.setenv("PAGETOMD_WIDE_TABLES", "html")
    monkeypatch.setenv("PAGETOMD_LOG_JSON", "true")
    monkeypatch.setenv("PAGETOMD_NO_FETCHED_AT", "true")

    cfg = Config.from_overrides({"url": "https://example.com"})

    assert cfg.timeout == 5.0
    assert cfg.retries == 7
    assert cfg.fetcher == "playwright"
    assert cfg.respect_robots is False
    assert cfg.wide_tables == "html"
    assert cfg.log_json is True
    assert cfg.no_fetched_at is True


def test_cli_overrides_take_precedence_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit CLI overrides should beat the env-sourced value."""
    monkeypatch.setenv("PAGETOMD_TIMEOUT", "5")
    cfg = Config.from_overrides({"url": "https://example.com", "timeout": 12.5})
    assert cfg.timeout == 12.5


def test_extra_fields_rejected() -> None:
    """Unknown fields surface as a ConfigError thanks to ``extra='forbid'``."""
    with pytest.raises(ConfigError) as info:
        Config.from_overrides({"url": "https://example.com", "nonsense": True})
    assert "errors" in info.value.context
    assert isinstance(info.value.__cause__, ValidationError)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", 0),
        ("timeout", -1.5),
        ("retries", -1),
        ("max_redirects", 0),
        ("max_redirects", -3),
    ],
)
def test_validators_reject_invalid_numbers(
    monkeypatch: pytest.MonkeyPatch, field: str, value: float
) -> None:
    """Numeric field validators raise via ConfigError for invalid inputs."""
    with pytest.raises(ConfigError):
        Config.from_overrides({"url": "https://example.com", field: value})


def test_config_instance_is_immutable() -> None:
    """Frozen models reject attribute mutation."""
    cfg = Config.from_overrides({"url": "https://example.com"})
    with pytest.raises(ValidationError):
        cfg.timeout = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fetcher", "curl"),
        ("heading_style", "underline"),
        ("wide_tables", "csv"),
        ("log_level", "trace"),
    ],
)
def test_literal_fields_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    """Literal fields reject any value outside their declared set."""
    with pytest.raises(ConfigError):
        Config.from_overrides({"url": "https://example.com", field: value})


def test_output_accepts_path() -> None:
    """The output field coerces strings to pathlib.Path."""
    cfg = Config.from_overrides({"url": "https://example.com", "output": "out.md"})
    assert cfg.output == pathlib.Path("out.md")


def _assert_user_agent_error(exc: ConfigError) -> None:
    """Assert a ConfigError originated from the ``user_agent`` field validator."""
    assert isinstance(exc.__cause__, ValidationError)
    errors = exc.context["errors"]
    assert isinstance(errors, list)
    assert any("user_agent" in err["loc"] for err in errors), errors


def test_user_agent_valid_value_accepted() -> None:
    """A clean single-line User-Agent should pass validation unchanged."""
    ua = "pagetomd/0.1 (+https://example.com)"
    cfg = Config.from_overrides({"url": "https://example.com", "user_agent": ua})
    assert cfg.user_agent == ua


@pytest.mark.parametrize("bad_char", ["\r", "\n", "\x00"])
def test_user_agent_rejects_control_characters(bad_char: str) -> None:
    """CR, LF, and NUL in the User-Agent must surface as ConfigError."""
    with pytest.raises(ConfigError) as info:
        Config.from_overrides({"url": "https://example.com", "user_agent": f"foo{bad_char}bar"})
    _assert_user_agent_error(info.value)


@pytest.mark.parametrize("value", ["", "   "])
def test_user_agent_rejects_empty_and_whitespace(value: str) -> None:
    """Empty or whitespace-only User-Agent strings must surface as ConfigError."""
    with pytest.raises(ConfigError) as info:
        Config.from_overrides({"url": "https://example.com", "user_agent": value})
    _assert_user_agent_error(info.value)
