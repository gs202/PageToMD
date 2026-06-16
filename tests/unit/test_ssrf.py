"""Unit tests for :mod:`pagetomd.ssrf`.

Every test clears ``PAGETOMD_INTERNAL_SKIP_SSRF`` so the production guard
code path is exercised.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from pagetomd.exceptions import FetchError
from pagetomd.ssrf import _resolve_addresses, guard_url, redact_url


@pytest.fixture(autouse=True)
def _clear_bypass(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the bypass env var and the ``_resolve_addresses`` LRU cache."""
    monkeypatch.delenv("PAGETOMD_INTERNAL_SKIP_SSRF", raising=False)
    _resolve_addresses.cache_clear()
    yield
    _resolve_addresses.cache_clear()


def _public_getaddrinfo(public_addr: str) -> Any:  # pragma: no cover - tiny factory wrapper
    """Build a ``getaddrinfo`` replacement that returns ``public_addr``."""

    def _fake(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (public_addr, port or 0))]

    return _fake


def test_public_ipv4_literal_allowed() -> None:
    """A public IPv4 literal passes the guard."""
    guard_url("https://1.1.1.1/")


def test_public_ipv6_literal_allowed() -> None:
    """A public IPv6 literal passes the guard."""
    guard_url("https://[2606:4700:4700::1111]/")


def test_public_hostname_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that resolves to a public IP passes the guard."""
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo("142.250.180.46"))
    guard_url("https://example.com/")


def test_loopback_ipv4_blocked() -> None:
    """``127.0.0.1`` is refused with the host captured in context."""
    with pytest.raises(FetchError) as exc_info:
        guard_url("http://127.0.0.1/")
    assert exc_info.value.context["host"] == "127.0.0.1"


def test_loopback_ipv6_blocked() -> None:
    """``[::1]`` is refused."""
    with pytest.raises(FetchError):
        guard_url("http://[::1]/")


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.0.1/",
    ],
)
def test_rfc1918_ranges_blocked(url: str) -> None:
    """All three RFC 1918 ranges are refused."""
    with pytest.raises(FetchError):
        guard_url(url)


def test_link_local_ipv4_blocked() -> None:
    """169.254.0.0/16 is refused."""
    with pytest.raises(FetchError):
        guard_url("http://169.254.1.1/")


def test_link_local_ipv6_blocked() -> None:
    """``fe80::/10`` is refused."""
    with pytest.raises(FetchError):
        guard_url("http://[fe80::1]/")


def test_unique_local_ipv6_blocked() -> None:
    """``fc00::/7`` is refused."""
    with pytest.raises(FetchError):
        guard_url("http://[fd00::1]/")


def test_unspecified_ipv4_blocked() -> None:
    """``0.0.0.0`` (unspecified) is refused."""
    with pytest.raises(FetchError):
        guard_url("http://0.0.0.0/")


def test_ipv4_hex_obfuscation_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``0x7f000001`` resolves to 127.0.0.1 via the resolver path."""
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo("127.0.0.1"))
    with pytest.raises(FetchError):
        guard_url("http://0x7f000001/")


def test_ipv4_decimal_obfuscation_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``2130706433`` resolves to 127.0.0.1 via the resolver path."""
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo("127.0.0.1"))
    with pytest.raises(FetchError):
        guard_url("http://2130706433/")


def test_ipv4_zero_padded_obfuscation_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """``127.000.000.001`` resolves to 127.0.0.1 via the resolver path."""
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo("127.0.0.1"))
    with pytest.raises(FetchError):
        guard_url("http://127.000.000.001/")


def test_cloud_metadata_aws_blocked() -> None:
    """AWS IMDS literal address is refused with a metadata-specific message."""
    with pytest.raises(FetchError) as exc_info:
        guard_url("http://169.254.169.254/latest/meta-data/")
    # Message must mention "metadata", "private", or "link" (user-facing API).
    msg = exc_info.value.message.lower()
    assert "metadata" in msg or "private" in msg or "link" in msg


def test_cloud_metadata_gcp_shortname_blocked() -> None:
    """``metadata.google.internal`` is refused without DNS resolution."""
    with pytest.raises(FetchError) as exc_info:
        guard_url("http://metadata.google.internal/")
    assert "metadata" in exc_info.value.message.lower()


def test_hostname_resolving_to_private_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that resolves to a private IP is refused with ``resolved`` set."""
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo("10.0.0.5"))
    with pytest.raises(FetchError) as exc_info:
        guard_url("http://internal.corp.example/")
    assert exc_info.value.context["resolved"] == "10.0.0.5"


def test_dns_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution failure leaves the guard quiet so the main fetch error wins."""

    def _boom(*args: object, **kwargs: object) -> None:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    guard_url("http://does-not-exist.example.invalid/")


def test_empty_host_does_not_raise() -> None:
    """Malformed URL with no host is left for the caller's own validation."""
    # ``urlsplit("not a url")`` returns an empty hostname. The guard
    # silently returns; the fetcher's ``_parse_url`` raises the
    # user-facing error.
    guard_url("not a url")


def test_bypass_env_var_disables_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """``PAGETOMD_INTERNAL_SKIP_SSRF=1`` disables the guard entirely."""
    monkeypatch.setenv("PAGETOMD_INTERNAL_SKIP_SSRF", "1")
    # Loopback would normally be blocked; bypass should silence it.
    guard_url("http://127.0.0.1/")


def test_bypass_env_var_only_literal_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any value other than literal ``"1"`` (e.g. ``"true"``) does NOT bypass."""
    monkeypatch.setenv("PAGETOMD_INTERNAL_SKIP_SSRF", "true")
    with pytest.raises(FetchError):
        guard_url("http://127.0.0.1/")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/path?q=1#f", "https://example.com/path?q=1#f"),
        ("https://alice@example.com/x", "https://example.com/x"),
        (  # pragma: allowlist secret
            "https://alice:secret@example.com/x",
            "https://example.com/x",
        ),
        (  # pragma: allowlist secret
            "https://alice:secret@example.com:8443/x",
            "https://example.com:8443/x",
        ),
        (  # pragma: allowlist secret
            "http://alice:secret@[2606:4700::1]:8080/x",
            "http://[2606:4700::1]:8080/x",
        ),
        ("not a url", "not a url"),
    ],
    ids=["no_userinfo", "user_only", "user_and_password", "preserves_port", "ipv6", "malformed"],
)
def test_redact_url(url: str, expected: str) -> None:
    assert redact_url(url) == expected
