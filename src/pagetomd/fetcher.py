"""HTTP fetcher implementations for :mod:`pagetomd`.

Provides :class:`HttpxFetcher` (synchronous, httpx-backed) and
:class:`PlaywrightFetcher` (headless Chromium) behind a common
:class:`Fetcher` protocol.
"""

from __future__ import annotations

import os
import re
import ssl
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)
from tenacity.wait import wait_base

from pagetomd.exceptions import DependencyMissingError, FetchError, RobotsDisallowedError
from pagetomd.logging import get_logger
from pagetomd.ssrf import guard_url, redact_url

if TYPE_CHECKING:  # pragma: no cover - import only used for type hints
    from pagetomd.config import Config

__all__ = [
    "FetchedDoc",
    "Fetcher",
    "HttpxFetcher",
    "PlaywrightFetcher",
]


# HTTP status codes that justify a retry: transient server / rate-limit signals
# only. 4xx other than these are treated as terminal client errors.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

# Status codes whose ``Retry-After`` header we honour. Per RFC 9110 §10.2.3
# the header is meaningful on 503 and 429 (and 3xx redirects, which httpx
# already follows transparently). For everything else we fall back to
# exponential backoff regardless of any Retry-After value the server sent.
_RETRY_AFTER_STATUSES: Final[frozenset[int]] = frozenset({429, 503})

# Hard cap on any ``Retry-After`` delay we honour, in seconds. Servers
# occasionally send absurdly long values (hours, days); honouring those
# would hang a crawl indefinitely on a single page. After the cap, we still
# wait the capped duration — the next attempt will fail again and increment
# the retry counter towards the per-page budget.
_RETRY_AFTER_CAP_SECONDS: Final[float] = 300.0

_DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_DEFAULT_ACCEPT_LANGUAGE = "en;q=0.9,*;q=0.5"

_ROBOTS_TIMEOUT_CAP = 5.0

# Max bytes we'll read from /robots.txt before aborting the stream. RFC 9309
# recommends a parser ceiling of at least 500 KB; we cap at 512 KB so
# a hostile server cannot stream a multi-GB body and exhaust memory. Oversize
# responses are treated as "no restriction" (same as a non-200 / unreachable
# robots.txt).
_ROBOTS_MAX_BYTES = 512 * 1024

# Maximum chained ``<meta http-equiv="refresh">`` hops we'll follow before
# giving up. HTTP-layer redirects are governed separately by httpx via
# ``Config.max_redirects``.
_META_REFRESH_HOP_CAP: Final[int] = 3

# Maximum delay (in seconds) we will honour from a ``<meta refresh>``
# directive. Longer delays are treated as a "you should bookmark this"
# hint rather than an immediate redirect and we ignore them.
_META_REFRESH_MAX_DELAY: Final[float] = 5.0

# Regex to extract the ``url=...`` segment of the ``content`` attribute of a
# ``<meta http-equiv="refresh">`` element. Permissive on whitespace and
# quoting; the surrounding parser asserts the ``http-equiv`` value.
_META_REFRESH_CONTENT_RE: Final[re.Pattern[str]] = re.compile(
    r"""^\s*([0-9]+(?:\.[0-9]+)?)        # delay (integer or float)
        \s*(?:;|,)\s*                     # delimiter
        url\s*=\s*                        # url= prefix
        ['"]?([^'"\s]+)['"]?              # the URL itself
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Whole-meta detector — locates the first ``<meta http-equiv="refresh" …>``
# element in the body, captures its ``content`` attribute. We deliberately
# do not depend on bs4 here so meta-refresh inspection is cheap.
_META_REFRESH_TAG_RE: Final[re.Pattern[str]] = re.compile(
    r"""<meta\b
        (?=[^>]*\bhttp-equiv\s*=\s*['"]?refresh['"]?\b)  # must be refresh
        [^>]*\bcontent\s*=\s*(['"])(?P<content>.*?)\1    # capture content
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

# Threshold above which a high density of U+FFFD (REPLACEMENT CHARACTER) is
# reported as a mojibake warning. We deliberately do NOT attempt to fix the
# encoding — surfacing the signal is enough.
_MOJIBAKE_DENSITY_THRESHOLD: Final[float] = 0.01  # 1% of characters

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FetchedDoc:
    """Immutable result of a successful HTTP fetch.

    Attributes:
        url: The originally requested URL, before any redirects.
        final_url: The URL after following all redirects; used by
            post-processing to resolve relative links.
        status_code: HTTP status code of the final response (always 2xx for a
            successful fetch — non-2xx is raised as :class:`FetchError`).
        html: The decoded response body as text.
        content_type: The raw ``Content-Type`` header value, or ``None`` if
            the server omitted it.
        encoding: Character encoding actually used to decode ``html``, or
            ``None`` if httpx could not determine one.
        headers: Read-only view over the response headers.
    """

    url: str
    final_url: str
    status_code: int
    html: str
    content_type: str | None
    encoding: str | None
    headers: Mapping[str, str]


class Fetcher(Protocol):
    """Structural type for any URL → :class:`FetchedDoc` adapter."""

    def fetch(self, url: str) -> FetchedDoc:
        """Fetch ``url`` and return the decoded document.

        Implementations must raise :class:`FetchError` (or its subclass
        :class:`RobotsDisallowedError`) for any failure — never leak raw
        httpx exceptions to callers.
        """
        ...


def _is_ssl_cert_error(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` wraps an SSL certificate verification failure.

    SSL certificate errors will never self-heal on retry, so they should be
    treated as terminal to avoid wasting time on exponential backoff.

    Walks both ``__cause__`` and ``__context__`` chains with cycle detection
    (some mocking libraries create circular exception chains).
    """
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return True
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__
    return False


def _is_retryable_exception(exc: BaseException) -> bool:
    """Return ``True`` when tenacity should retry after seeing ``exc``.

    Transport errors are retried unless they wrap an SSL certificate
    verification failure (which will never self-heal). HTTP status errors
    are retried only for the curated set of transient codes; everything
    else (e.g. ``404``) is terminal.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status_code: int = exc.response.status_code
        return status_code in _RETRYABLE_STATUSES
    if isinstance(exc, httpx.TransportError):
        return not _is_ssl_cert_error(exc)
    return False


def _is_retryable_playwright_exception(exc: BaseException) -> bool:
    """Return ``True`` when tenacity should retry a Playwright fetch.

    Two categories qualify: ``FetchError`` instances stamped with a
    ``status_code`` in :data:`_RETRYABLE_STATUSES` (e.g. 429/503 from
    ``page.goto``), and ``FetchError`` instances wrapping a navigation
    failure (timeout, connection reset) that can plausibly self-heal on
    retry.  All other errors (SSRF refusals, robots disallow, etc.) are
    terminal and surface immediately.
    """
    if not isinstance(exc, FetchError):
        return False
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        return bool(status_code in _RETRYABLE_STATUSES)
    # No status_code attribute → not an HTTP-status FetchError.  Retry only
    # when the message indicates a navigation failure (the other
    # ``raise FetchError(...)`` site in PlaywrightFetcher).
    return exc.message.startswith("Playwright navigation failed")


class SSRFSafeTransport(httpx.HTTPTransport):
    """SSRF-safe HTTP transport that prevents DNS rebinding attacks.

    Resolves the hostname once, validates the IP address, and rewrites the
    request URL to use the validated IP address directly. Passes the original
    hostname in the ``Host`` header and configures SSL/TLS verification
    against the original hostname using the ``sni_hostname`` extension.
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        original_url = request.url
        original_host = request.url.host
        if request.url.port is not None:
            original_host = f"{original_host}:{request.url.port}"

        # Guard and resolve the URL to a validated public IP address.
        validated_ip = guard_url(str(original_url))

        if validated_ip:
            # Rewrite the request URL to use the validated IP address directly.
            request.headers["Host"] = original_host
            request.extensions["sni_hostname"] = request.url.host
            request.url = request.url.copy_with(host=validated_ip)

        try:
            response = super().handle_request(request)
        finally:
            # Restore the original URL so that response.url and any redirect
            # handling/logging see the original URL instead of the IP address.
            request.url = original_url

        return response


class HttpxFetcher:
    """Synchronous :class:`Fetcher` backed by :class:`httpx.Client`.

    Supports transient (one-shot) and reusable (context-manager) modes.
    Robots cache is per-instance, keyed by ``(scheme, host, port)``.
    """

    def __init__(self, config: Config) -> None:
        """Capture configuration; defer client creation until needed."""
        self._config = config
        self._client: httpx.Client | None = None
        self._robots_cache: dict[tuple[str, str, int], RobotFileParser | None] = {}

    def __enter__(self) -> HttpxFetcher:
        """Open a reusable :class:`httpx.Client` for the duration of the block."""
        self._client = self._build_client()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        """Close the reusable client (if any)."""
        self.close()

    def close(self) -> None:
        """Close and drop the persistent client, if one is open."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def fetch(self, url: str) -> FetchedDoc:
        """Fetch ``url`` and return a :class:`FetchedDoc`.

        Args:
            url: Absolute ``http``/``https`` URL.

        Returns:
            A populated :class:`FetchedDoc` for the successful response.

        Raises:
            FetchError: When the URL is invalid, when robots disallows the
                URL (subclass :class:`RobotsDisallowedError`), the body is
                larger than ``Config.max_body_bytes``, or when the HTTP
                request fails after retries.
        """
        # Use the persistent client if we're inside a context manager,
        # otherwise build a one-shot client and tear it down at the end.
        own_client = self._client is None
        client = self._client if self._client is not None else self._build_client()
        try:
            return self._fetch_with_meta_refresh(client, url)
        finally:
            if own_client:
                client.close()

    def _fetch_with_meta_refresh(self, client: httpx.Client, url: str) -> FetchedDoc:
        """Fetch ``url`` and transparently follow body-level meta-refresh hops.

        Capped at :data:`_META_REFRESH_HOP_CAP` hops; each hop is
        re-guarded against SSRF.
        """
        current_url = url
        last_doc: FetchedDoc | None = None
        for hop in range(_META_REFRESH_HOP_CAP + 1):
            parsed = self._parse_url(current_url)
            guard_url(current_url)
            if self._config.respect_robots:
                self._check_robots(client, parsed)
            doc = self._do_get(client, current_url)
            last_doc = doc

            if not self._config.follow_redirects or hop == _META_REFRESH_HOP_CAP:
                return doc

            target = _detect_meta_refresh(doc.html, doc.final_url)
            if target is None:
                return doc
            _log.info(
                "fetch.meta_refresh",
                from_url=redact_url(doc.final_url),
                to_url=redact_url(target),
                hop=hop + 1,
            )
            current_url = target
        assert last_doc is not None  # pragma: no cover - unreachable
        return last_doc

    def _build_client(self) -> httpx.Client:
        """Construct an :class:`httpx.Client` with SSRF-guarded redirect hooks."""
        cfg = self._config
        transport = SSRFSafeTransport(
            verify=cfg.verify_ssl,
        )
        return httpx.Client(
            transport=transport,
            timeout=cfg.timeout,
            verify=cfg.verify_ssl,
            follow_redirects=cfg.follow_redirects,
            max_redirects=cfg.max_redirects,
            headers={
                "User-Agent": cfg.user_agent,
                "Accept": _DEFAULT_ACCEPT,
                "Accept-Language": _DEFAULT_ACCEPT_LANGUAGE,
            },
            event_hooks={"response": [_guard_redirect_response]},
        )

    def _parse_url(self, url: str) -> _ParsedUrl:
        """Validate ``url`` and return its split components.

        Raises:
            FetchError: For empty input, non-``http(s)`` schemes, or missing
                netloc.
        """
        if not url or not isinstance(url, str):
            raise FetchError("URL is empty")
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise FetchError(f"Malformed URL: {exc}") from exc

        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise FetchError(f"Unsupported URL scheme: {parts.scheme!r}")
        if not parts.netloc:
            raise FetchError("URL has no host component")

        # urlsplit returns ``hostname`` lowercased and without auth/port,
        # exactly what we want for robots cache keying.
        return _ParsedUrl(
            scheme=scheme,
            hostname=parts.hostname or "",
            port=parts.port,
            path=parts.path or "/",
            raw=url,
        )

    def _check_robots(self, client: httpx.Client, parsed: _ParsedUrl) -> None:
        """Enforce ``robots.txt`` for ``parsed.raw``."""
        key = (parsed.scheme, parsed.hostname, parsed.port or _default_port(parsed.scheme))
        parser = self._get_or_fetch_robots(client, parsed, key)
        if parser is None:
            # No reachable / parseable robots.txt → treat as unrestricted.
            return

        ua = self._config.user_agent
        allowed = parser.can_fetch(ua, parsed.raw)
        _log.debug("robots.check", url=redact_url(parsed.raw), allowed=allowed)
        if not allowed:
            raise RobotsDisallowedError(
                f"robots.txt disallows {redact_url(parsed.raw)}",
            )

    def _get_or_fetch_robots(
        self,
        client: httpx.Client,
        parsed: _ParsedUrl,
        key: tuple[str, str, int],
    ) -> RobotFileParser | None:
        """Return the cached parser for ``key`` or fetch it now."""
        if key in self._robots_cache:
            return self._robots_cache[key]

        host_part = parsed.hostname
        if parsed.port is not None:
            host_part = f"{host_part}:{parsed.port}"
        robots_url = f"{parsed.scheme}://{host_part}/robots.txt"

        timeout = min(_ROBOTS_TIMEOUT_CAP, self._config.timeout)
        try:
            buf = bytearray()
            truncated = False
            with client.stream(
                "GET",
                robots_url,
                timeout=timeout,
                follow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    _log.debug(
                        "robots.fetch_non_200",
                        url=redact_url(robots_url),
                        status_code=resp.status_code,
                    )
                    self._robots_cache[key] = None
                    return None
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > _ROBOTS_MAX_BYTES:
                        truncated = True
                        break
        except httpx.HTTPError as exc:
            _log.debug("robots.fetch_failed", url=redact_url(robots_url), error=str(exc))
            self._robots_cache[key] = None
            return None

        if truncated:
            _log.warning(
                "robots.fetch_oversized",
                url=redact_url(robots_url),
                host=parsed.hostname,
                port=parsed.port,
                limit_bytes=_ROBOTS_MAX_BYTES,
            )
            self._robots_cache[key] = None
            return None

        text = bytes(buf).decode("utf-8", errors="replace")
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        self._robots_cache[key] = parser
        return parser

    def _do_get(self, client: httpx.Client, url: str) -> FetchedDoc:
        """Issue the GET with tenacity-managed retries.

        Wraps any final failure in a rich :class:`FetchError`.

        Body size is enforced incrementally during streaming so that a
        compressed response (e.g. a gzip bomb) cannot exhaust memory before
        the cap fires.  A ``Content-Length`` pre-check is still performed
        as a cheap early-exit for uncompressed responses whose headers are
        trustworthy; it does not protect against chunked-encoded or
        compressed transfers, hence the streaming cap is always applied.
        """
        attempts = self._config.retries + 1
        attempt_holder: list[int] = [0]

        # Mutable list used as a simple out-param from the retry closure so
        # that metadata captured inside the stream context is available after
        # the stream closes.  Populated on every successful attempt so that
        # the last value is always valid when Retrying returns.
        _result: list[tuple[int, str, str | None, str | None, dict[str, str], bytes]] = []

        def _one_attempt() -> None:
            attempt_holder[0] += 1
            cap = self._config.max_body_bytes
            with client.stream("GET", url) as resp:
                # --- Content-Length pre-check (cheap early exit) ---
                cl_raw = resp.headers.get("Content-Length")
                if cl_raw is not None:
                    try:
                        cl = int(cl_raw)
                    except ValueError:
                        cl = -1
                    if cl > cap:
                        raise FetchError(f"Body exceeds {cap} byte cap")

                resp.raise_for_status()

                # --- Streaming body read with inline size cap ---
                # Accumulate decompressed bytes chunk by chunk so the cap
                # fires before the full body is in memory — defeating gzip
                # bombs and other compressed payloads that would otherwise
                # slip past the Content-Length pre-check.
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > cap:
                        raise FetchError(f"Body exceeds {cap} byte cap")

                _result.clear()
                _result.append(
                    (
                        resp.status_code,
                        str(resp.url),
                        resp.headers.get("Content-Type"),
                        resp.encoding,
                        dict(resp.headers),
                        bytes(buf),
                    )
                )

        retrying = Retrying(
            stop=stop_after_attempt(attempts),
            wait=_WaitRetryAfterOrExponential(
                url,
                wait_exponential(multiplier=2, min=2, max=60),
            ),
            retry=retry_if_exception(_is_retryable_exception),
            reraise=True,
            before_sleep=_make_retry_logger(url, attempts),
        )

        try:
            retrying(_one_attempt)
        except httpx.HTTPStatusError as exc:
            safe_url = redact_url(url)
            raise FetchError(
                f"HTTP {exc.response.status_code} for {safe_url}",
            ) from exc
        except httpx.HTTPError as exc:
            safe_url = redact_url(url)
            err = FetchError(
                f"Transport error fetching {safe_url}: {exc}",
            )
            if _is_ssl_cert_error(exc):
                err.hint = (
                    "TLS certificate verification failed. If you are behind a "
                    "corporate proxy, re-run with --no-verify-ssl."
                )
            raise err from exc

        status_code, final_url, content_type, encoding, headers_dict, raw_body = _result[0]
        body_text = raw_body.decode(encoding or "utf-8", errors="replace")
        self._warn_if_non_html(content_type, url)
        _warn_on_mojibake(body_text, url)
        _log.info(
            "fetch.ok",
            url=redact_url(url),
            status_code=status_code,
            final_url=redact_url(final_url),
        )
        headers_proxy: Mapping[str, str] = types.MappingProxyType(headers_dict)
        return FetchedDoc(
            url=url,
            final_url=final_url,
            status_code=status_code,
            html=body_text,
            content_type=content_type,
            encoding=encoding,
            headers=headers_proxy,
        )

    @staticmethod
    def _warn_if_non_html(content_type: str | None, url: str) -> None:
        """Emit a warning when the body is unlikely to be HTML/XML."""
        if not content_type:
            _log.debug("fetch.no_content_type", url=redact_url(url))
            return
        ct = content_type.lower()
        if "html" not in ct and "xml" not in ct:
            _log.warning(
                "fetch.non_html_content_type", url=redact_url(url), content_type=content_type
            )


@dataclass(frozen=True, slots=True)
class _ParsedUrl:
    """Internal split of a validated URL used by the fetcher's helpers."""

    scheme: str
    hostname: str
    port: int | None
    path: str
    raw: str


def _default_port(scheme: str) -> int:
    """Return the well-known port for ``scheme`` (used only for cache keys)."""
    return 443 if scheme == "https" else 80


def _make_retry_logger(url: str, total_attempts: int) -> Callable[[RetryCallState], None]:
    """Build a tenacity ``before_sleep`` hook that logs each retry attempt.

    Logs at ``info`` level so retry progress is visible in default runs
    (``debug`` was effectively invisible to operators watching a slow
    crawl).  ``total_attempts`` is rendered as the denominator of an
    ``attempt/total`` pair so the user can see how close they are to the
    per-page retry budget at a glance.
    """

    def _hook(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        error: str | None = None
        if outcome is not None and outcome.failed:
            error = repr(outcome.exception())
        next_wait = (
            round(retry_state.next_action.sleep, 2) if retry_state.next_action is not None else None
        )
        _log.info(
            "fetch.retry",
            url=redact_url(url),
            attempt=f"{retry_state.attempt_number}/{total_attempts}",
            next_wait_s=next_wait,
            error=error,
        )

    return _hook


def _parse_retry_after(value: str, *, now: datetime | None = None) -> float | None:
    """Parse an HTTP ``Retry-After`` header value into seconds.

    The header may be either an integer number of seconds (``"30"``) or an
    HTTP-date (``"Wed, 21 Oct 2015 07:28:00 GMT"``) per RFC 9110 §10.2.3.
    Returns ``None`` if the value cannot be parsed; the caller is expected
    to fall back to the exponential-backoff schedule.

    Args:
        value: Raw header value as returned by the server.
        now: Reference instant for date-form parsing. Exposed so tests can
            pin a value; defaults to :func:`datetime.now` in UTC.

    Returns:
        Non-negative seconds to wait, or ``None`` if unparseable.
    """
    text = value.strip()
    if not text:
        return None
    # Try integer-seconds form first (most common in practice).
    seconds: float | None
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        return max(0.0, seconds)

    # Fall back to HTTP-date form.
    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    # ``parsedate_to_datetime`` returns a naive datetime when no timezone
    # is present; treat naive values as UTC per RFC 9110's IMF-fixdate.
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(tz=UTC)
    delta = (target - reference).total_seconds()
    return max(0.0, delta)


class _WaitRetryAfterOrExponential(wait_base):
    """Honour ``Retry-After`` on 429/503; otherwise use exponential backoff.

    Per-page retry budgets are still bounded by :class:`stop_after_attempt`
    in the outer ``Retrying`` configuration. This strategy only governs the
    *duration* of each sleep, not the *number* of attempts.
    """

    _jitter: wait_random = wait_random(0, 1)

    def __init__(self, url: str, exponential: wait_exponential) -> None:
        self._url = url
        self._exponential = exponential

    def __call__(self, retry_state: RetryCallState) -> float:
        """Return the seconds to sleep before the next retry attempt.

        Jitter (0-1 s uniform) is added to every computed wait to spread
        concurrent retries and avoid thundering-herd effects.
        """
        exponential_wait = self._exponential(retry_state) + self._jitter(retry_state)
        outcome = retry_state.outcome
        if outcome is None or not outcome.failed:
            return exponential_wait
        exc = outcome.exception()
        if not isinstance(exc, httpx.HTTPStatusError):
            return exponential_wait
        if exc.response.status_code not in _RETRY_AFTER_STATUSES:
            return exponential_wait
        header = exc.response.headers.get("Retry-After")
        if header is None:
            return exponential_wait
        parsed = _parse_retry_after(header)
        if parsed is None:
            return exponential_wait
        capped = min(parsed, _RETRY_AFTER_CAP_SECONDS)
        # Never sleep less than the exponential schedule would have asked
        # for: if the server requests 1 s but we are already on attempt 4
        # of exponential backoff (≈8 s), the server's value is too
        # optimistic and would likely re-trigger the same 429.
        chosen = max(capped, exponential_wait)
        _log.info(
            "fetch.retry_after",
            url=redact_url(self._url),
            header_value=header,
            parsed_seconds=parsed,
            chosen_seconds=chosen,
            capped=parsed > _RETRY_AFTER_CAP_SECONDS,
        )
        return chosen


def _detect_meta_refresh(html: str, base_url: str) -> str | None:
    """Return the absolute target URL of a body-level meta-refresh, if any.

    Returns ``None`` when no eligible refresh is present or delay exceeds
    :data:`_META_REFRESH_MAX_DELAY`.
    """
    if not html:
        return None
    search_space = html[:50_000]
    head_match = re.search(r"<head\b[^>]*>(.*?)</head>", search_space, re.IGNORECASE | re.DOTALL)
    haystack = head_match.group(1) if head_match else search_space

    tag_match = _META_REFRESH_TAG_RE.search(haystack)
    if tag_match is None:
        return None
    content_match = _META_REFRESH_CONTENT_RE.match(tag_match.group("content"))
    if content_match is None:
        return None
    delay = float(content_match.group(1))
    if delay > _META_REFRESH_MAX_DELAY:
        return None
    target = content_match.group(2).strip()
    if not target:
        return None
    resolved: str = urljoin(base_url, target)
    return resolved


def _warn_on_mojibake(text: str, url: str) -> None:
    """Emit a warning when the U+FFFD density in ``text`` exceeds the threshold."""
    length = len(text)
    if length < 100:
        return
    bad = text.count("\ufffd")
    if bad == 0:
        return
    density = bad / length
    if density >= _MOJIBAKE_DENSITY_THRESHOLD:
        _log.warning(
            "fetch.mojibake_detected",
            url=redact_url(url),
            replacement_chars=bad,
            text_length=length,
            density=density,
        )


def _guard_redirect_response(response: httpx.Response) -> None:
    """httpx ``response`` hook that re-applies :func:`guard_url` on ``3xx`` redirects."""
    if response.status_code < 300 or response.status_code >= 400:
        return
    location = response.headers.get("location")
    if not location:
        return
    target = urljoin(str(response.request.url), location)
    guard_url(target)


_PLAYWRIGHT_DEP_MESSAGE = (
    "Playwright is not installed. Install with: "
    "uv tool install 'pagetomd[playwright]' && playwright install chromium"
)

# Launch hardening: cap V8 heap, avoid /dev/shm issues, disable timer throttling.
# NOTE: ``--no-zygote`` was removed because newer Chromium rejects it when the
# sandbox is enabled (``Zygote cannot be disabled if sandbox is enabled``).
_CHROMIUM_LAUNCH_ARGS: Final[tuple[str, ...]] = (
    "--js-flags=--max-old-space-size=512",
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
)


# JavaScript that serializes the full DOM including shadow roots into a single
# HTML string by walking the *live* tree recursively. ``cloneNode`` does not
# copy shadow roots, so we must traverse the live nodes and inline each
# shadow root's children directly. Only a safe subset of attributes is kept
# (href, src, alt, title, class, id) to keep the output compact.
_SHADOW_DOM_SERIALIZER: Final[str] = """
() => {
    const _SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
    const _VOID = new Set(['area','base','br','col','embed','hr','img','input',
                           'link','meta','param','source','track','wbr']);
    const _KEEP_ATTRS = new Set(['href','src','alt','title','class','id','name','content']);

    function ser(node) {
        if (node.nodeType === Node.TEXT_NODE) return node.textContent || '';
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        const tag = node.tagName;
        if (_SKIP.has(tag)) return '';
        const tagL = tag.toLowerCase();
        let attrs = '';
        for (const a of node.attributes) {
            if (_KEEP_ATTRS.has(a.name)) {
                attrs += ' ' + a.name + '="' + a.value.replace(/"/g, '&quot;') + '"';
            }
        }
        let inner = '';
        if (node.shadowRoot) {
            for (const c of node.shadowRoot.childNodes) inner += ser(c);
        }
        for (const c of node.childNodes) inner += ser(c);
        if (_VOID.has(tagL)) return '<' + tagL + attrs + '>';
        return '<' + tagL + attrs + '>' + inner + '</' + tagL + '>';
    }

    try {
        return '<!DOCTYPE html><html>' + ser(document.documentElement) + '</html>';
    } catch(e) {
        return null;
    }
}
"""


class PlaywrightFetcher:
    """Synchronous Playwright-based fetcher for JavaScript-rendered pages.

    Renders SPA pages via headless Chromium. Delegates ``robots.txt``
    checks to an internal :class:`HttpxFetcher`. Supports transient and
    reusable (context-manager) modes mirroring :class:`HttpxFetcher`.
    """

    def __init__(self, config: Config) -> None:
        """Capture configuration and resolve the Playwright entry point."""
        self._config = config
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise DependencyMissingError(_PLAYWRIGHT_DEP_MESSAGE) from exc
        self._sync_playwright = sync_playwright
        self._robots_delegate = HttpxFetcher(config)
        self._playwright_cm: object | None = None
        self._browser: object | None = None

    def __enter__(self) -> PlaywrightFetcher:
        """Launch Chromium once for the duration of the ``with`` block.

        Also enters the robots-delegate so its persistent :class:`httpx.Client`
        is initialised once and reused across all robots checks, avoiding a
        fresh TLS handshake per page.
        """
        self._robots_delegate.__enter__()
        cm = self._sync_playwright()
        playwright = cm.__enter__()
        self._playwright_cm = cm
        self._browser = playwright.chromium.launch(
            headless=True,
            chromium_sandbox=not bool(os.environ.get("CI")),
            args=list(_CHROMIUM_LAUNCH_ARGS),
        )
        _log.debug("fetch.playwright.browser.launched", mode="context_manager")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        """Close the reusable browser, playwright instance, and robots delegate."""
        self.close()

    def close(self) -> None:
        """Close the persistent browser + playwright instance, if any."""
        if self._browser is not None:
            try:
                self._browser.close()  # type: ignore[attr-defined]
            finally:
                self._browser = None
        if self._playwright_cm is not None:
            try:
                # ``PlaywrightContextManager.__exit__`` tears down the
                # background driver process started by ``__enter__``.
                self._playwright_cm.__exit__(None, None, None)  # type: ignore[attr-defined]
            finally:
                self._playwright_cm = None
            _log.debug("fetch.playwright.browser.closed", mode="context_manager")
        # Close the robots delegate, which also shuts down its persistent
        # httpx client when PlaywrightFetcher was used as a context manager.
        self._robots_delegate.close()

    def fetch(self, url: str) -> FetchedDoc:
        """Render ``url`` in Chromium and return the post-render HTML.

        Raises :class:`FetchError` on SSRF, robots, or navigation failure.
        """
        parsed = self._robots_delegate._parse_url(url)
        guard_url(url)
        self._check_robots_via_httpx(parsed)

        own_playwright = self._browser is None

        if own_playwright:
            with self._sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    chromium_sandbox=not bool(os.environ.get("CI")),
                    args=list(_CHROMIUM_LAUNCH_ARGS),
                )
                _log.debug("fetch.playwright.browser.launched", mode="transient")
                try:
                    return self._render(browser, url)
                finally:
                    browser.close()
                    _log.debug("fetch.playwright.browser.closed", mode="transient")
        else:
            return self._render(self._browser, url)

    def _check_robots_via_httpx(self, parsed: _ParsedUrl) -> None:
        """Reuse :class:`HttpxFetcher`'s robots logic without launching Chromium.

        When ``PlaywrightFetcher`` is used as a context manager, the delegate's
        persistent ``_client`` is already open (initialised in ``__enter__``)
        and is reused here, avoiding a fresh TLS handshake per page.  In
        transient (one-shot) mode the delegate has no persistent client, so a
        short-lived client is built and closed as before.
        """
        if not self._config.respect_robots:
            return
        delegate = self._robots_delegate
        if delegate._client is not None:
            # Reuse the persistent client opened in __enter__.
            delegate._check_robots(delegate._client, parsed)
            return
        # Transient mode: build a one-shot client.
        client = delegate._build_client()
        try:
            delegate._check_robots(client, parsed)
        finally:
            client.close()

    def _render(self, browser: object, url: str) -> FetchedDoc:
        """Render ``url`` with tenacity-managed retries on transient HTTP errors.

        Mirrors :meth:`HttpxFetcher._do_get` so ``--retries N`` applies
        uniformly to both fetchers.  Retries fire for 429/503/500/etc. and
        for navigation failures (timeouts, connection resets); terminal
        errors like SSRF refusals or robots disallow surface immediately.
        ``Retry-After`` on 429/503 is honoured via the same wait strategy
        the httpx fetcher uses.
        """
        attempts = self._config.retries + 1
        attempt_holder: list[int] = [0]
        result_holder: list[FetchedDoc] = []

        def _one_attempt() -> None:
            attempt_holder[0] += 1
            result_holder.clear()
            result_holder.append(self._render_once(browser, url))

        retrying = Retrying(
            stop=stop_after_attempt(attempts),
            wait=_WaitRetryAfterOrExponential(
                url,
                wait_exponential(multiplier=2, min=2, max=60),
            ),
            retry=retry_if_exception(_is_retryable_playwright_exception),
            reraise=True,
            before_sleep=_make_retry_logger(url, attempts),
        )

        try:
            retrying(_one_attempt)
        except FetchError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise FetchError(f"Playwright render failed: {exc}") from exc

        return result_holder[0]

    def _render_once(self, browser: object, url: str) -> FetchedDoc:
        """Drive the browser, capture the rendered HTML, wrap errors."""
        from playwright.sync_api import Error as PlaywrightError

        cfg = self._config
        timeout_ms = int(cfg.timeout * 1000)
        try:
            context = browser.new_context(  # type: ignore[attr-defined]
                user_agent=cfg.user_agent,
                ignore_https_errors=not cfg.verify_ssl,
            )
            context.set_default_navigation_timeout(timeout_ms)
            page = context.new_page()
            try:
                response = page.goto(url, wait_until="networkidle")
                page.wait_for_timeout(cfg.playwright_idle_ms)
                final_url = page.url
                status_code = response.status if response is not None else 200
                # Surface HTTP errors as FetchError so the pipeline classifies
                # them correctly (exit 2) and crawl mode counts them under
                # ``failed_urls`` instead of misclassifying the error page's
                # empty content as an ``ExtractionEmptyError``.  Mirrors the
                # behaviour of ``HttpxFetcher._do_get`` which calls
                # ``raise_for_status()``.
                if status_code >= 400:
                    safe_url = redact_url(url)
                    _log.warning(
                        "fetch.playwright.http_error",
                        url=safe_url,
                        final_url=redact_url(final_url),
                        status_code=status_code,
                        retryable=status_code in _RETRYABLE_STATUSES,
                    )
                    err = FetchError(f"HTTP {status_code} for {safe_url}")
                    # Stamp the status code on the exception so the retry
                    # predicate in ``_render`` can decide whether to retry
                    # without parsing the message string.
                    err.status_code = status_code  # type: ignore[attr-defined]
                    if status_code in _RETRYABLE_STATUSES:
                        err.hint = (
                            f"Server returned {status_code}. The site may be rate-limiting "
                            "the crawl; reduce concurrency, increase --retries, or wait and retry."
                        )
                    raise err
                html = page.evaluate(_SHADOW_DOM_SERIALIZER) or page.content()
                headers = dict(response.headers) if response is not None else {}
            finally:
                page.close()
                context.close()
        except PlaywrightError as exc:
            _log.error(
                "fetch.playwright.error",
                url=redact_url(url),
                error=str(exc),
            )
            raise FetchError("Playwright navigation failed") from exc

        _log.info(
            "fetch.playwright.ok",
            url=redact_url(url),
            final_url=redact_url(final_url),
            status_code=status_code,
        )
        headers_proxy: Mapping[str, str] = types.MappingProxyType(dict(headers))
        return FetchedDoc(
            url=url,
            final_url=final_url,
            status_code=status_code,
            html=html,
            content_type="text/html",
            encoding="utf-8",
            headers=headers_proxy,
        )
