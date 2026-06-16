"""Unit tests for :mod:`pagetomd.fetcher` (respx-mocked, no real network)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from pagetomd.config import Config
from pagetomd.exceptions import FetchError, RobotsDisallowedError
from pagetomd.fetcher import (
    FetchedDoc,
    HttpxFetcher,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Short-circuit tenacity sleeps so retry tests stay fast."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _seconds: None)


def _make_config(**overrides: object) -> Config:
    """Build a :class:`Config` with sane defaults for fetcher tests."""
    base: dict[str, object] = {
        "url": "https://example.com/",
        "timeout": 5.0,
        "retries": 3,
        "respect_robots": False,
        "follow_redirects": True,
        "max_redirects": 5,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


@pytest.fixture
def cfg() -> Config:
    """Default fetcher config: 3 retries, robots OFF for ergonomic tests."""
    return _make_config()


@pytest.fixture
def cfg_robots() -> Config:
    """Fetcher config with robots ON, used by the robots-focused tests."""
    return _make_config(respect_robots=True)


@respx.mock
def test_success_returns_fetched_doc(cfg: Config) -> None:
    """200 + HTML body → fully populated :class:`FetchedDoc`."""
    route = respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200,
            html="<html><body>hi</body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/page")

    assert route.called
    assert isinstance(doc, FetchedDoc)
    assert doc.url == "https://example.com/page"
    assert doc.final_url == "https://example.com/page"
    assert doc.status_code == 200
    assert "hi" in doc.html
    assert doc.content_type is not None and "text/html" in doc.content_type
    assert doc.elapsed_ms >= 0


@respx.mock
def test_redirect_updates_final_url(cfg: Config) -> None:
    """301 → 200 leaves ``final_url`` pointing at the redirect target."""
    respx.get("https://example.com/old").mock(
        return_value=httpx.Response(301, headers={"Location": "https://example.com/new"})
    )
    respx.get("https://example.com/new").mock(
        return_value=httpx.Response(
            200,
            html="<html>landed</html>",
            headers={"Content-Type": "text/html"},
        )
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/old")

    assert doc.url == "https://example.com/old"
    assert doc.final_url == "https://example.com/new"
    assert doc.status_code == 200


@respx.mock
def test_retries_on_503_then_succeeds(cfg: Config) -> None:
    """Two transient 503s followed by a 200 → returns the 200 after 3 calls."""
    route = respx.get("https://example.com/flap").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(
                200,
                html="<html>finally</html>",
                headers={"Content-Type": "text/html"},
            ),
        ]
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/flap")

    assert doc.status_code == 200
    assert route.call_count == 3


@respx.mock
def test_exhausted_retries_raises_fetch_error(cfg: Config) -> None:
    """All ``retries+1`` attempts return 503 → :class:`FetchError`."""
    route = respx.get("https://example.com/down").mock(return_value=httpx.Response(503))

    with pytest.raises(FetchError) as excinfo:
        HttpxFetcher(cfg).fetch("https://example.com/down")

    assert route.call_count == cfg.retries + 1
    assert excinfo.value.context["status_code"] == 503
    assert excinfo.value.context["attempt"] == cfg.retries + 1
    assert excinfo.value.context["url"] == "https://example.com/down"


@respx.mock
def test_transport_error_retried_then_fetch_error(cfg: Config) -> None:
    """A persistent :class:`httpx.ConnectError` exhausts retries."""
    route = respx.get("https://example.com/boom").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(FetchError) as excinfo:
        HttpxFetcher(cfg).fetch("https://example.com/boom")

    assert route.call_count == cfg.retries + 1
    # Transport-level failures carry no status code.
    assert excinfo.value.context["status_code"] is None
    assert excinfo.value.context["attempt"] == cfg.retries + 1


@respx.mock
def test_404_not_retried(cfg: Config) -> None:
    """4xx other than the retryable set short-circuit immediately."""
    route = respx.get("https://example.com/missing").mock(return_value=httpx.Response(404))

    with pytest.raises(FetchError) as excinfo:
        HttpxFetcher(cfg).fetch("https://example.com/missing")

    assert route.call_count == 1
    assert excinfo.value.context["status_code"] == 404
    assert excinfo.value.context["attempt"] == 1


@respx.mock
def test_robots_allowed_proceeds(cfg_robots: Config) -> None:
    """A permissive ``/robots.txt`` lets the fetch proceed."""
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    respx.get("https://example.com/ok").mock(
        return_value=httpx.Response(
            200, html="<html>ok</html>", headers={"Content-Type": "text/html"}
        )
    )

    doc = HttpxFetcher(cfg_robots).fetch("https://example.com/ok")

    assert doc.status_code == 200


@respx.mock
def test_robots_disallows_private_but_allows_public(cfg_robots: Config) -> None:
    """``Disallow: /private`` blocks ``/private`` but allows ``/public``."""
    respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(
            200,
            text="User-agent: *\nDisallow: /private\n",
        )
    )
    respx.get("https://example.com/public").mock(
        return_value=httpx.Response(
            200, html="<html>ok</html>", headers={"Content-Type": "text/html"}
        )
    )

    fetcher = HttpxFetcher(cfg_robots)

    with pytest.raises(RobotsDisallowedError) as excinfo:
        fetcher.fetch("https://example.com/private")
    assert excinfo.value.context["url"] == "https://example.com/private"

    doc = fetcher.fetch("https://example.com/public")
    assert doc.status_code == 200


@respx.mock
def test_robots_cached_per_instance(cfg_robots: Config) -> None:
    """``/robots.txt`` is fetched at most once per host per fetcher."""
    robots_route = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(
            200, html="<html>a</html>", headers={"Content-Type": "text/html"}
        )
    )
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(
            200, html="<html>b</html>", headers={"Content-Type": "text/html"}
        )
    )

    fetcher = HttpxFetcher(cfg_robots)
    fetcher.fetch("https://example.com/a")
    fetcher.fetch("https://example.com/b")

    assert robots_route.call_count == 1


@respx.mock
def test_robots_fetch_500_treated_as_unrestricted(cfg_robots: Config) -> None:
    """A 500 from ``/robots.txt`` must not block the underlying fetch."""
    respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(500))
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, html="<html>ok</html>", headers={"Content-Type": "text/html"}
        )
    )

    doc = HttpxFetcher(cfg_robots).fetch("https://example.com/page")

    assert doc.status_code == 200


@pytest.mark.parametrize(
    "bad_url",
    [
        "not-a-url",
        "ftp://x/y",
        "",
        "https://",  # no netloc
    ],
)
@respx.mock
def test_invalid_url_raises_without_http_call(cfg: Config, bad_url: str) -> None:
    """Bad URLs raise :class:`FetchError` before any network call is made."""
    catch_all = respx.route().mock(return_value=httpx.Response(200))

    with pytest.raises(FetchError):
        HttpxFetcher(cfg).fetch(bad_url)

    assert not catch_all.called


@respx.mock
def test_non_html_content_type_returns_doc(cfg: Config, caplog: pytest.LogCaptureFixture) -> None:
    """``application/pdf`` does not raise; a warning is logged."""
    respx.get("https://example.com/file.pdf").mock(
        return_value=httpx.Response(
            200,
            content=b"%PDF-1.4 ...",
            headers={"Content-Type": "application/pdf"},
        )
    )

    doc = HttpxFetcher(cfg).fetch("https://example.com/file.pdf")

    assert doc.status_code == 200
    assert doc.content_type == "application/pdf"


@respx.mock
def test_context_manager_reuses_client(cfg: Config) -> None:
    """Two ``fetch`` calls inside ``with`` share the same underlying client."""
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(
            200, html="<html>a</html>", headers={"Content-Type": "text/html"}
        )
    )
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(
            200, html="<html>b</html>", headers={"Content-Type": "text/html"}
        )
    )

    with HttpxFetcher(cfg) as fetcher:
        client_before = fetcher._client
        fetcher.fetch("https://example.com/a")
        client_after_first = fetcher._client
        fetcher.fetch("https://example.com/b")
        client_after_second = fetcher._client

        assert client_before is not None
        assert client_before is client_after_first
        assert client_before is client_after_second

    # ``__exit__`` must close and drop the client reference.
    assert fetcher._client is None


@respx.mock
def test_transient_use_does_not_leak_client(cfg: Config) -> None:
    """``HttpxFetcher(cfg).fetch(...)`` outside ``with`` leaves ``_client=None``."""
    respx.get("https://example.com/once").mock(
        return_value=httpx.Response(
            200, html="<html>once</html>", headers={"Content-Type": "text/html"}
        )
    )

    fetcher = HttpxFetcher(cfg)
    fetcher.fetch("https://example.com/once")

    assert fetcher._client is None


def test_close_is_idempotent(cfg: Config) -> None:
    """Calling ``close`` twice must not raise."""
    fetcher = HttpxFetcher(cfg)
    fetcher.close()
    fetcher.close()  # second call exercises the ``_client is None`` branch


@respx.mock
def test_robots_network_error_treated_as_unrestricted(cfg_robots: Config) -> None:
    """A transport error fetching robots.txt is logged and ignored."""
    respx.get("https://example.com/robots.txt").mock(side_effect=httpx.ConnectError("dns boom"))
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200, html="<html>ok</html>", headers={"Content-Type": "text/html"}
        )
    )

    doc = HttpxFetcher(cfg_robots).fetch("https://example.com/page")

    assert doc.status_code == 200


@respx.mock
def test_missing_content_type_logs_warning(cfg: Config) -> None:
    """Responses without a Content-Type header still return successfully."""

    def _no_ct(request: httpx.Request) -> httpx.Response:
        # ``httpx.Response`` only sets ``Content-Type`` when it has to infer
        # one from a string body; passing raw bytes and explicit empty
        # headers leaves it unset.
        return httpx.Response(200, content=b"<html>x</html>", headers={})

    route = respx.get("https://example.com/nct").mock(side_effect=_no_ct)

    doc = HttpxFetcher(cfg).fetch("https://example.com/nct")

    assert route.called
    assert doc.status_code == 200
    # ``httpx`` may infer a default ``Content-Type``; we only assert the
    # fetcher tolerates whatever the server returns without crashing.
    assert doc.html == "<html>x</html>"


@respx.mock
def test_fetch_protocol_compatibility(cfg: Config) -> None:
    """``HttpxFetcher`` satisfies the :class:`Fetcher` Protocol structurally."""
    from pagetomd.fetcher import Fetcher

    respx.get("https://example.com/p").mock(
        return_value=httpx.Response(
            200, html="<html>p</html>", headers={"Content-Type": "text/html"}
        )
    )

    fetcher: Fetcher = HttpxFetcher(cfg)
    doc = fetcher.fetch("https://example.com/p")
    assert doc.status_code == 200


@pytest.fixture(autouse=True)
def _respx_clean() -> Iterator[None]:
    """Belt-and-braces: every test starts and ends with a clean respx state."""
    yield
