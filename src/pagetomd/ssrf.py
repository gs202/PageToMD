"""SSRF guard — refuse fetches of private and cloud-metadata addresses.

Two test-only escape hatches exist, both unreachable in production:

1. **In-process tests** — ``monkeypatch.setattr(pagetomd.ssrf, "_BYPASS", True)``.
   The ``_ssrf_bypass`` autouse fixture in ``tests/conftest.py`` does this for
   every test that runs inside the pytest process.

2. **Subprocess tests** — integration tests spawn ``pagetomd`` as a real
   subprocess (via ``subprocess.run``).  ``monkeypatch`` cannot reach a child
   process.  For these, set ``PAGETOMD_INTERNAL_SKIP_SSRF=1`` in the child's
   environment **and** ensure ``PYTEST_CURRENT_TEST`` is present (pytest always
   sets this in the parent and child processes inherit it).  The double gate
   makes the bypass physically unreachable in production where
   ``PYTEST_CURRENT_TEST`` is never set.

There is **no** supported production-reachable way to disable this guard.
Do not add one.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from pagetomd.exceptions import FetchError

__all__ = ["guard_url", "redact_url"]

_log = logging.getLogger(__name__)

# In-process test-only escape hatch.  Set exclusively via monkeypatch.setattr;
# never set this to True in application or library code.
_BYPASS: bool = False

# Env-var name for the subprocess escape hatch (integration tests only).
# Effective only when PYTEST_CURRENT_TEST is also present in the environment
# so it cannot fire in production.
_INTERNAL_BYPASS_ENV = "PAGETOMD_INTERNAL_SKIP_SSRF"


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
    # In-process bypass (monkeypatch.setattr in unit/snapshot/property tests).
    if _BYPASS:
        _log.warning("ssrf.guard_disabled")
        return None

    # Subprocess bypass: only honoured when PYTEST_CURRENT_TEST is present,
    # which pytest always sets in its worker processes.  This makes the bypass
    # physically unreachable in production where that variable is never set.
    if os.environ.get(_INTERNAL_BYPASS_ENV) == "1" and os.environ.get("PYTEST_CURRENT_TEST"):
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
