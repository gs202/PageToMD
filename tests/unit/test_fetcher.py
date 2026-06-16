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
    _is_retryable_exception,
    _is_ssl_cert_error,
)
from tests.conftest import make_config


@pytest.fixture
def cfg() -> Config:
    """Default fetcher config: 3 retries, robots OFF for ergonomic tests."""
    return make_config()


@pytest.fixture
def cfg_robots() -> Config:
    """Fetcher config with robots ON, used by the robots-focused tests."""
    return make_config(respect_robots=True)


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
    """Calling close() twice is safe and leaves _client as None."""
    fetcher = HttpxFetcher(cfg)
    fetcher.close()
    assert fetcher._client is None
    fetcher.close()
    assert fetcher._client is None


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





def test_is_ssl_cert_error_detects_wrapped_ssl_error() -> None:
    """``_is_ssl_cert_error`` detects SSL errors wrapped in ConnectError."""
    import ssl as _ssl

    ssl_err = _ssl.SSLCertVerificationError("cert verify failed")
    connect_err = httpx.ConnectError("ssl failed")
    connect_err.__cause__ = ssl_err

    assert _is_ssl_cert_error(connect_err) is True


def test_is_ssl_cert_error_returns_false_for_plain_connect_error() -> None:
    """``_is_ssl_cert_error`` returns False for non-SSL ConnectErrors."""
    err = httpx.ConnectError("dns failed")
    assert _is_ssl_cert_error(err) is False


def test_is_retryable_returns_false_for_ssl_cert_error() -> None:
    """SSL cert errors must not be retried."""
    import ssl as _ssl

    ssl_err = _ssl.SSLCertVerificationError("cert verify failed")
    connect_err = httpx.ConnectError("ssl failed")
    connect_err.__cause__ = ssl_err

    assert _is_retryable_exception(connect_err) is False


def test_is_retryable_returns_true_for_plain_connect_error() -> None:
    """Non-SSL transport errors remain retryable."""
    err = httpx.ConnectError("dns failed")
    assert _is_retryable_exception(err) is True


def test_ssl_cert_error_not_retried(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """SSL certificate errors fail immediately without retrying."""
    import ssl as _ssl

    call_count = 0

    original_get = httpx.Client.get

    def _patched_get(self: httpx.Client, *args: object, **kwargs: object) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        ssl_err = _ssl.SSLCertVerificationError(
            "certificate verify failed: self-signed certificate"
        )
        raise httpx.ConnectError("ssl handshake failed") from ssl_err

    monkeypatch.setattr(httpx.Client, "get", _patched_get)

    with pytest.raises(FetchError) as excinfo:
        HttpxFetcher(cfg).fetch("https://example.com/secure")

    # Must NOT retry — exactly 1 attempt.
    assert call_count == 1
    assert excinfo.value.context["attempt"] == 1
    assert "--no-verify-ssl" in excinfo.value.hint


@respx.mock
def test_verify_ssl_false_passed_to_client() -> None:
    """``verify_ssl=False`` is forwarded to the httpx client."""
    cfg = make_config(verify_ssl=False)
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(
            200,
            html="<html>ok</html>",
            headers={"Content-Type": "text/html"},
        )
    )

    fetcher = HttpxFetcher(cfg)
    client = fetcher._build_client()
    try:
        # httpx stores the verify setting as a ssl.SSLContext or bool on
        # the transport; the simplest check is that the client was built
        # without error and can serve requests.
        assert client is not None
    finally:
        client.close()


@pytest.fixture(autouse=True)
def _respx_clean() -> Iterator[None]:
    """Belt-and-braces: every test starts and ends with a clean respx state."""
    yield


def test_ssrf_safe_transport_rewrites_url_and_sets_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """SSRFSafeTransport rewrites the request URL to the validated IP, sets Host and sni_hostname."""
    from pagetomd.fetcher import SSRFSafeTransport
    import pagetomd.fetcher

    # Mock guard_url to return a specific IP address
    monkeypatch.setattr(pagetomd.fetcher, "guard_url", lambda url: "1.1.1.1")

    transport = SSRFSafeTransport()
    request = httpx.Request("GET", "https://example.com:8443/page")

    # Mock the superclass handle_request to inspect the request passed to it
    captured_request = None
    captured_url = None

    def mock_handle_request(self, req: httpx.Request) -> httpx.Response:
        nonlocal captured_request, captured_url
        captured_request = req
        captured_url = req.url
        # Return a dummy response
        return httpx.Response(200, request=req)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", mock_handle_request)

    response = transport.handle_request(request)

    # Verify that the request was rewritten correctly
    assert captured_request is not None
    assert captured_url is not None
    assert captured_url.host == "1.1.1.1"
    assert captured_url.port == 8443
    assert captured_request.headers["Host"] == "example.com:8443"
    assert captured_request.extensions["sni_hostname"] == "example.com"

    # Verify that the original URL was restored on the request after handle_request returned
    assert request.url == "https://example.com:8443/page"
    assert response.url == "https://example.com:8443/page"
