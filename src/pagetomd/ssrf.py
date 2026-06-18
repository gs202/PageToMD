"""SSRF guard — refuse fetches of private and cloud-metadata addresses.

The test-only escape hatch is the module-level ``_BYPASS`` flag.  Tests that
need to reach loopback addresses set it via ``monkeypatch.setattr``::

    monkeypatch.setattr(pagetomd.ssrf, "_BYPASS", True)

There is **no** production-reachable way to disable this guard.  Do not add
one.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from pagetomd.exceptions import FetchError

__all__ = ["guard_url", "redact_url"]

_log = logging.getLogger(__name__)

# Test-only escape hatch.  Set exclusively via monkeypatch.setattr in tests;
# never set this to True in application or library code.
_BYPASS: bool = False


def redact_url(url: str) -> str:
    """Strip ``userinfo`` (user:password@) from a URL for safe logging.

    Returns the input unchanged when parsing fails or when the URL has no
    parseable hostname.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.hostname:
        return url
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    netloc = f"{host}:{parts.port}" if parts.port is not None else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# Cloud-metadata hosts that must always be refused. The IP literals are
# included because some classifiers miss them; the hostnames are included
# because resolving them yields the metadata IP and we want to fail fast
# before any DNS round-trip.
_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / OpenStack IMDS
        "fd00:ec2::254",  # AWS IPv6 IMDS
        "metadata.google.internal",  # GCP
        "metadata.goog",  # GCP shortname
    }
)


@lru_cache(maxsize=256)
def _resolve_addresses(host: str, port: int | None) -> tuple[str, ...]:
    """Resolve ``host`` to a tuple of unique address strings.

    Returns an empty tuple on resolution failure.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return ()
    return tuple({str(info[4][0]) for info in infos})


def _is_private_address(addr: str) -> bool:
    """Return ``True`` when ``addr`` is in any reserved / private range.

    Non-IP input is treated as suspicious and returns ``True``.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        # Unresolvable text — treat as suspicious to fail closed.
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def guard_url(url: str) -> str | None:
    """Refuse to fetch ``url`` when it targets any private / metadata address.

    Checks metadata hostnames, IP literals, and all DNS-resolved addresses.
    Raises :class:`FetchError` when the target is non-public.

    Returns:
        The validated IP address (or original host if it's an IP literal),
        or ``None`` if the bypass is active or the URL has no host.
    """
    if _BYPASS:
        _log.warning("ssrf.guard_disabled")
        return None

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return None

    if host in _METADATA_HOSTS:
        raise FetchError("Refusing to fetch cloud metadata service")

    port = parts.port  # may be None; getaddrinfo accepts None for any port

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_private_address(str(literal)):
            raise FetchError(
                "Refusing to fetch private/loopback/link-local address",
            )
        return str(literal)

    addrs = _resolve_addresses(host, port)
    if not addrs:
        return None

    for addr in addrs:
        if _is_private_address(addr):
            raise FetchError(
                "Refusing to fetch host that resolves to a private address",
            )

    return addrs[0]
