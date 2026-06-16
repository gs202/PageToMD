"""Edge-case unit tests for :mod:`pagetomd.fetcher` (meta-refresh, mojibake, oversized body)."""

from __future__ import annotations

import httpx
import pytest
import respx

from pagetomd.config import Config
from pagetomd.exceptions import ConfigError, FetchError, RobotsDisallowedError
from pagetomd.fetcher import HttpxFetcher


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same tenacity-sleep neutering as the main fetcher tests."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _seconds: None)


def _make_config(**overrides: object) -> Config:
    """Build a fetcher-test :class:`Config` with the usual safe defaults."""
    base: dict[str, object] = {
        "url": "https://example.com/",
        "timeout": 5.0,
        "retries": 1,
        "respect_robots": False,
        "follow_redirects": True,
        "max_redirects": 5,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


@respx.mock
def test_meta_refresh_follows_body_redirect() -> None:
    """A ``<meta http-equiv="refresh">`` body redirect chases the target."""
    cfg = _make_config()
    redirect_body = (
        '<html><head><meta http-equiv="refresh" content="0; url=/real">'
        "</head><body>landing</body></html>"
    )
    respx.get("https://example.com/start").mock(
        return_value=httpx.Response(200, html=redirect_body, headers={"Content-Type": "text/html"})
    )
    real_route = respx.get("https://example.com/real").mock(
        return_value=httpx.Response(
            200,
            html="<html><body>final</body></html>",
            headers={"Content-Type": "text/html"},
        )
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/start")

    assert real_route.called
    assert doc.final_url == "https://example.com/real"
    assert "final" in doc.html


@respx.mock
def test_meta_refresh_resolves_relative_url_against_final_url() -> None:
    """Relative meta-refresh targets resolve against ``final_url``."""
    cfg = _make_config()
    redirect_body = (
        '<html><head><meta http-equiv="refresh" content="1; url=child.html">'
        "</head><body>x</body></html>"
    )
    respx.get("https://example.com/dir/page").mock(
        return_value=httpx.Response(200, html=redirect_body, headers={"Content-Type": "text/html"})
    )
    target = respx.get("https://example.com/dir/child.html").mock(
        return_value=httpx.Response(
            200,
            html="<html><body>final</body></html>",
            headers={"Content-Type": "text/html"},
        )
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/dir/page")
    assert target.called
    assert doc.final_url == "https://example.com/dir/child.html"


@respx.mock
def test_meta_refresh_ignored_when_delay_too_long() -> None:
    """A delay above 5 s is a bookmark hint, not an immediate redirect."""
    cfg = _make_config()
    body = (
        '<html><head><meta http-equiv="refresh" content="30; url=/elsewhere">'
        "</head><body>stay</body></html>"
    )
    respx.get("https://example.com/slow").mock(
        return_value=httpx.Response(200, html=body, headers={"Content-Type": "text/html"})
    )
    elsewhere = respx.get("https://example.com/elsewhere").mock(
        return_value=httpx.Response(200, html="<html></html>")
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/slow")
    assert not elsewhere.called
    assert doc.final_url == "https://example.com/slow"


@respx.mock
def test_meta_refresh_hop_cap_aborts_after_three() -> None:
    """The fourth meta-refresh hop is treated as terminal, not infinite."""
    cfg = _make_config()

    def refresh_to(target: str) -> str:
        return (
            f'<html><head><meta http-equiv="refresh" content="0; url={target}">'
            "</head><body>x</body></html>"
        )

    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(
            200, html=refresh_to("/b"), headers={"Content-Type": "text/html"}
        )
    )
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(
            200, html=refresh_to("/c"), headers={"Content-Type": "text/html"}
        )
    )
    respx.get("https://example.com/c").mock(
        return_value=httpx.Response(
            200, html=refresh_to("/d"), headers={"Content-Type": "text/html"}
        )
    )
    last = respx.get("https://example.com/d").mock(
        return_value=httpx.Response(
            200, html=refresh_to("/e"), headers={"Content-Type": "text/html"}
        )
    )
    respx.get("https://example.com/e").mock(return_value=httpx.Response(200, html="<html></html>"))

    doc = HttpxFetcher(cfg).fetch("https://example.com/a")
    # The 4th hop fetches /d successfully; the loop refuses to continue.
    assert last.called
    assert doc.final_url == "https://example.com/d"


@respx.mock
def test_meta_refresh_disabled_when_follow_redirects_false() -> None:
    """``follow_redirects=False`` disables the meta-refresh chase too."""
    body = (
        '<html><head><meta http-equiv="refresh" content="0; url=/x"></head><body>stay</body></html>'
    )
    cfg = _make_config(follow_redirects=False)
    respx.get("https://example.com/").mock(
        return_value=httpx.Response(200, html=body, headers={"Content-Type": "text/html"})
    )
    x_route = respx.get("https://example.com/x").mock(
        return_value=httpx.Response(200, html="<html></html>")
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/")
    assert not x_route.called
    assert doc.final_url == "https://example.com/"


@respx.mock
def test_mojibake_density_above_threshold_logs_warning() -> None:
    """A body dense in U+FFFD logs ``fetch.mojibake_detected``."""
    from structlog.testing import capture_logs

    cfg = _make_config()
    body = "ascii filler " * 20 + ("\ufffd" * 20)
    respx.get("https://example.com/mojibake").mock(
        return_value=httpx.Response(200, html=body, headers={"Content-Type": "text/html"})
    )

    with capture_logs() as cap:
        HttpxFetcher(cfg).fetch("https://example.com/mojibake")

    events = {entry["event"] for entry in cap}
    assert "fetch.mojibake_detected" in events


@respx.mock
def test_clean_body_does_not_log_mojibake() -> None:
    """A clean body produces no mojibake warning."""
    from structlog.testing import capture_logs

    cfg = _make_config()
    respx.get("https://example.com/clean").mock(
        return_value=httpx.Response(
            200,
            html="<html><body>" + ("clean text " * 50) + "</body></html>",
            headers={"Content-Type": "text/html"},
        )
    )

    with capture_logs() as cap:
        HttpxFetcher(cfg).fetch("https://example.com/clean")

    events = {entry["event"] for entry in cap}
    assert "fetch.mojibake_detected" not in events


def test_warn_on_mojibake_skips_tiny_bodies() -> None:
    """Bodies under 100 chars skip the density check entirely."""
    from pagetomd.fetcher import _warn_on_mojibake

    # No raise, no log. We just want the function to be a no-op for tiny
    # inputs (length floor avoids tripping on a single replacement).
    _warn_on_mojibake("\ufffd" * 50, "https://x/")


@respx.mock
def test_content_length_header_exceeds_cap_raises_fetch_error() -> None:
    """A Content-Length above the cap raises ``FetchError`` early."""
    cfg = _make_config(max_body_bytes=100)
    respx.get("https://example.com/big").mock(
        return_value=httpx.Response(
            200,
            content=b"<html>tiny</html>",
            headers={
                "Content-Type": "text/html",
                "Content-Length": "10000",  # well above 100
            },
        )
    )

    with pytest.raises(FetchError) as excinfo:
        HttpxFetcher(cfg).fetch("https://example.com/big")
    assert excinfo.value.context["content_length"] == 10000
    assert excinfo.value.context["max_body_bytes"] == 100


@respx.mock
def test_actual_body_exceeds_cap_raises_fetch_error() -> None:
    """When Content-Length is absent, the body size is enforced post-fetch."""
    cfg = _make_config(max_body_bytes=50)
    body = b"x" * 5000

    def _build(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Type": "text/html"})

    respx.get("https://example.com/chunked").mock(side_effect=_build)

    with pytest.raises(FetchError) as excinfo:
        HttpxFetcher(cfg).fetch("https://example.com/chunked")
    assert excinfo.value.context["content_length"] == 5000


def test_max_body_bytes_must_be_positive() -> None:
    """``Config`` rejects a non-positive ``max_body_bytes``."""
    with pytest.raises(ConfigError):
        Config.from_overrides({"url": "https://example.com/", "max_body_bytes": 0})
    with pytest.raises(ConfigError):
        Config.from_overrides({"url": "https://example.com/", "max_body_bytes": -1})


def test_detect_meta_refresh_returns_none_for_empty_input() -> None:
    """Empty / non-html / no-match input returns ``None``."""
    from pagetomd.fetcher import _detect_meta_refresh

    assert _detect_meta_refresh("", "https://x/") is None
    assert _detect_meta_refresh("<html><body>no meta</body></html>", "https://x/") is None
    assert (
        _detect_meta_refresh(
            '<head><meta http-equiv="refresh" content="abc"></head>',
            "https://x/",
        )
        is None
    )


def test_detect_meta_refresh_falls_back_to_full_body_without_head() -> None:
    """When ``<head>`` is missing the regex still scans the full document."""
    from pagetomd.fetcher import _detect_meta_refresh

    body = '<meta http-equiv="refresh" content="0; url=/x"><body>y</body>'
    assert _detect_meta_refresh(body, "https://example.com/") == "https://example.com/x"


def _robots_cfg() -> Config:
    """Config with robots ON — needed to exercise the streaming cap."""
    return _make_config(respect_robots=True)


@respx.mock
def test_robots_oversized_treated_as_unrestricted_with_warning() -> None:
    """A multi-MB robots.txt is aborted, logged, and treated as no restriction."""
    from structlog.testing import capture_logs

    from pagetomd.fetcher import _ROBOTS_MAX_BYTES

    cfg = _robots_cfg()
    # Body above cap; would disallow /page.html if honoured.
    huge_body = b"User-agent: *\nDisallow: /\n" * 50_000
    assert len(huge_body) > _ROBOTS_MAX_BYTES
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, content=huge_body)
    )
    respx.get("https://example.com/page.html").mock(
        return_value=httpx.Response(
            200,
            html="<html><body>hi</body></html>",
            headers={"Content-Type": "text/html"},
        )
    )

    fetcher = HttpxFetcher(cfg)
    with capture_logs() as events:
        doc = fetcher.fetch("https://example.com/page.html")

    # Fetch proceeds because oversized robots → cache None → unrestricted.
    assert doc.status_code == 200

    oversize_events = [
        e
        for e in events
        if e.get("event") == "robots.fetch_oversized" and e.get("log_level") == "warning"
    ]
    assert len(oversize_events) == 1
    assert oversize_events[0]["host"] == "example.com"
    assert oversize_events[0]["limit_bytes"] == _ROBOTS_MAX_BYTES

    # Cache holds the sentinel ``None`` for this host.
    key = ("https", "example.com", 443)
    assert key in fetcher._robots_cache
    assert fetcher._robots_cache[key] is None


@respx.mock
def test_robots_exactly_at_limit_parsed_normally() -> None:
    """A body of exactly ``_ROBOTS_MAX_BYTES`` is parsed, not truncated."""
    from pagetomd.fetcher import _ROBOTS_MAX_BYTES

    cfg = _robots_cfg()
    # Build a valid robots.txt body of exactly _ROBOTS_MAX_BYTES bytes.
    rule = b"User-agent: *\nDisallow: /private\n"
    padding_len = _ROBOTS_MAX_BYTES - len(rule) - len(b"# \n")
    body = rule + b"# " + (b"X" * padding_len) + b"\n"
    assert len(body) == _ROBOTS_MAX_BYTES

    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(200, content=body))
    respx.get("https://example.com/public").mock(
        return_value=httpx.Response(
            200, html="<html>ok</html>", headers={"Content-Type": "text/html"}
        )
    )

    fetcher = HttpxFetcher(cfg)
    # Rule was honoured: /private is blocked.
    with pytest.raises(RobotsDisallowedError):
        fetcher.fetch("https://example.com/private")
    # And /public still works (parser is functional, not skipped).
    doc = fetcher.fetch("https://example.com/public")
    assert doc.status_code == 200


@respx.mock
def test_robots_one_byte_over_limit_triggers_warning() -> None:
    """``_ROBOTS_MAX_BYTES + 1`` bytes trips the inclusive cap check."""
    from structlog.testing import capture_logs

    from pagetomd.fetcher import _ROBOTS_MAX_BYTES

    cfg = _robots_cfg()
    body = b"X" * (_ROBOTS_MAX_BYTES + 1)
    assert len(body) == _ROBOTS_MAX_BYTES + 1

    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(200, content=body))
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, html="<html>ok</html>", headers={"Content-Type": "text/html"}
        )
    )

    fetcher = HttpxFetcher(cfg)
    with capture_logs() as events:
        doc = fetcher.fetch("https://example.com/page")

    assert doc.status_code == 200
    assert any(
        e.get("event") == "robots.fetch_oversized" and e.get("log_level") == "warning"
        for e in events
    )


@respx.mock
def test_fetch_error_url_context_redacts_userinfo() -> None:
    """A 500 response on a userinfo URL produces a FetchError without credentials."""
    cfg = _make_config()
    respx.get("https://example.com/x").mock(
        return_value=httpx.Response(500, html="boom", headers={"Content-Type": "text/html"})
    )

    with pytest.raises(FetchError) as excinfo:
        HttpxFetcher(cfg).fetch("https://alice:secret@example.com/x")

    captured_url = excinfo.value.context["url"]
    assert isinstance(captured_url, str)
    assert "alice" not in captured_url
    assert "secret" not in captured_url
    assert "example.com/x" in captured_url
    # The FetchError message itself also embeds the URL — must be clean too.
    assert "alice" not in excinfo.value.message
    assert "secret" not in excinfo.value.message


@respx.mock
def test_successful_fetch_does_not_log_userinfo() -> None:
    """A 200 fetch with userinfo emits zero log records mentioning credentials."""
    from structlog.testing import capture_logs

    cfg = _make_config()
    respx.get("https://example.com/x").mock(
        return_value=httpx.Response(
            200,
            html="<html><body>ok</body></html>",
            headers={"Content-Type": "text/html"},
        )
    )

    with capture_logs() as cap:
        HttpxFetcher(cfg).fetch("https://alice:secret@example.com/x")

    for entry in cap:
        for value in entry.values():
            text = str(value)
            assert "alice" not in text, f"credential leaked in log entry: {entry!r}"
            assert "secret" not in text, f"credential leaked in log entry: {entry!r}"
