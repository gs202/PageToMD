"""Unit tests for the ``Retry-After`` header handling in the fetcher.

Covers both the standalone :func:`_parse_retry_after` parser and the
end-to-end behaviour of :class:`HttpxFetcher` when a server returns
429/503 with a ``Retry-After`` header. Retries themselves are
instantaneous in tests thanks to the ``_no_sleep`` fixture in
``tests/unit/conftest.py`` which monkey-patches ``tenacity.nap.time.sleep``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from tenacity import RetryCallState, wait_exponential

from pagetomd.config import Config
from pagetomd.exceptions import FetchError
from pagetomd.fetcher import (
    _RETRY_AFTER_CAP_SECONDS,
    HttpxFetcher,
    _parse_retry_after,
    _WaitRetryAfterOrExponential,
)
from tests.conftest import make_config

# ---------------------------------------------------------------------------
# _parse_retry_after — pure helper, no I/O
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    """Cover both forms of the header per RFC 9110 §10.2.3."""

    def test_integer_seconds(self) -> None:
        assert _parse_retry_after("30") == 30.0

    def test_float_seconds_accepted(self) -> None:
        # httpx itself never emits floats, but tolerate them in case some
        # exotic server does — the parser must not crash.
        assert _parse_retry_after("1.5") == 1.5

    def test_whitespace_tolerated(self) -> None:
        assert _parse_retry_after("  10  ") == 10.0

    def test_negative_clamped_to_zero(self) -> None:
        # A negative delay would let tenacity busy-loop; clamp to 0.
        assert _parse_retry_after("-5") == 0.0

    def test_empty_returns_none(self) -> None:
        assert _parse_retry_after("") is None

    def test_unparseable_returns_none(self) -> None:
        assert _parse_retry_after("soon") is None

    def test_http_date_in_the_future(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        future = now + timedelta(seconds=45)
        header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        result = _parse_retry_after(header, now=now)
        assert result is not None
        # Allow ±1 s tolerance for date formatting rounding.
        assert abs(result - 45.0) < 1.0

    def test_http_date_in_the_past_clamped_to_zero(self) -> None:
        # If the deadline has already passed, retry immediately rather
        # than negative-sleeping.
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        past = now - timedelta(seconds=30)
        header = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert _parse_retry_after(header, now=now) == 0.0


# ---------------------------------------------------------------------------
# _WaitRetryAfterOrExponential — strategy unit tests
# ---------------------------------------------------------------------------


def _make_state_for_status(
    status_code: int,
    *,
    retry_after: str | None,
    attempt: int = 1,
) -> RetryCallState:
    """Build a tenacity ``RetryCallState`` carrying an ``HTTPStatusError``.

    We construct the state directly rather than running a full retry loop —
    the wait strategy reads only ``retry_state.outcome.exception()`` plus
    ``retry_state.attempt_number``, so a hand-built state with a populated
    outcome is sufficient and far faster than a respx round-trip.
    """
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    response = httpx.Response(
        status_code,
        request=httpx.Request("GET", "https://example.com/x"),
        headers=headers,
    )
    exc = httpx.HTTPStatusError("status", request=response.request, response=response)

    outcome = MagicMock()
    outcome.failed = True
    outcome.exception = MagicMock(return_value=exc)

    state = MagicMock(spec=RetryCallState)
    state.outcome = outcome
    state.attempt_number = attempt
    state.seconds_since_start = 0.0
    state.idle_for = 0.0
    return state


class TestWaitRetryAfterOrExponential:
    def setup_method(self) -> None:
        self.exp = wait_exponential(multiplier=1, min=1, max=8)
        self.wait = _WaitRetryAfterOrExponential("https://example.com/x", self.exp)

    def test_429_with_retry_after_honoured(self) -> None:
        state = _make_state_for_status(429, retry_after="30", attempt=1)
        # Jitter adds up to 1 s on top of the Retry-After value.
        assert 30.0 <= self.wait(state) <= 31.0

    def test_503_with_retry_after_honoured(self) -> None:
        state = _make_state_for_status(503, retry_after="15", attempt=1)
        assert 15.0 <= self.wait(state) <= 16.0

    def test_retry_after_capped_at_5_minutes(self) -> None:
        # Servers occasionally emit absurd values; we cap to avoid hanging
        # a crawl indefinitely on a single page.
        state = _make_state_for_status(429, retry_after="9999", attempt=1)
        result = self.wait(state)
        assert _RETRY_AFTER_CAP_SECONDS <= result <= _RETRY_AFTER_CAP_SECONDS + 1

    def test_retry_after_floor_is_exponential_value(self) -> None:
        # On attempt 4 the exponential schedule asks for ~8 s. If the
        # server says 1 s, we honour the exponential floor instead — its
        # 1 s would likely re-trigger the same rate limit.
        state = _make_state_for_status(429, retry_after="1", attempt=4)
        result = self.wait(state)
        assert result >= 8.0

    def test_500_ignores_retry_after_uses_exponential(self) -> None:
        # 500 is retryable but not in our Retry-After-honouring set
        # (per RFC the header is defined for 503 and 429, not 5xx in
        # general). Fall back to exponential.
        state = _make_state_for_status(500, retry_after="60", attempt=1)
        result = self.wait(state)
        assert result < 60.0  # exponential gave us something ≤ 1 s on attempt 1

    def test_missing_retry_after_falls_back_to_exponential(self) -> None:
        state = _make_state_for_status(429, retry_after=None, attempt=1)
        result = self.wait(state)
        # Jitter adds up to 1 s on top of the exponential base; confirm we
        # did not blow up trying to parse a missing header.
        base = self.exp(state)
        assert base <= result <= base + 1

    def test_unparseable_retry_after_falls_back_to_exponential(self) -> None:
        state = _make_state_for_status(429, retry_after="soon", attempt=1)
        result = self.wait(state)
        base = self.exp(state)
        assert base <= result <= base + 1


# ---------------------------------------------------------------------------
# End-to-end fetcher behaviour with respx
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg() -> Config:
    """Default test config: robots OFF, retries left at the project default."""
    return make_config()


@respx.mock
def test_429_with_retry_after_then_success(cfg: Config) -> None:
    """One 429 + Retry-After followed by a 200 succeeds on attempt 2."""
    route = respx.get("https://example.com/limited").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(
                200,
                html="<html>ok</html>",
                headers={"Content-Type": "text/html"},
            ),
        ]
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/limited")

    assert doc.status_code == 200
    assert route.call_count == 2


@respx.mock
def test_429_exhausts_retries_then_fails(cfg: Config) -> None:
    """A persistent 429 with Retry-After still exhausts the per-page budget."""
    route = respx.get("https://example.com/blocked").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )

    with pytest.raises(FetchError) as excinfo:
        HttpxFetcher(cfg).fetch("https://example.com/blocked")

    assert route.call_count == cfg.retries + 1
    assert "429" in excinfo.value.message


@respx.mock
def test_429_without_retry_after_still_retried(cfg: Config) -> None:
    """A 429 without the header is retried via the exponential fallback."""
    route = respx.get("https://example.com/nohdr").mock(
        side_effect=[
            httpx.Response(429),  # no Retry-After
            httpx.Response(
                200,
                html="<html>ok</html>",
                headers={"Content-Type": "text/html"},
            ),
        ]
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/nohdr")

    assert doc.status_code == 200
    assert route.call_count == 2
