"""Endpoint validation: HTTPS enforcement, allowlisting, SSRF and redirect checks, size limits."""
from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Iterable, Optional
from urllib.parse import urlsplit

from .errors import SecurityError

Resolver = Callable[..., list]


def _host_allowed(host: str, allowlist: Optional[Iterable[str]]) -> bool:
    if allowlist is None:
        return True
    host = host.lower()
    for entry in allowlist:
        entry = entry.lower().lstrip(".")
        if host == entry or host.endswith("." + entry):
            return True
    return False


def _ip_is_public(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    ) and ip.is_global


def validate_endpoint(
    url: str,
    allowlist: Optional[Iterable[str]] = None,
    allow_http: bool = False,
    resolve: bool = True,
    resolver: Resolver = socket.getaddrinfo,
) -> str:
    """Validate an endpoint URL; returns the URL unchanged or raises SecurityError."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise SecurityError("invalid endpoint")
    parts = urlsplit(url)
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in allowed_schemes:
        raise SecurityError(f"scheme not allowed: {parts.scheme!r}")
    if parts.username or parts.password:
        raise SecurityError("credentials in URL are not allowed")
    host = parts.hostname
    if not host:
        raise SecurityError("missing host")
    try:
        port = parts.port
    except ValueError as exc:
        raise SecurityError("invalid port") from exc
    if not _host_allowed(host, allowlist):
        raise SecurityError(f"host not in allowlist: {host}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _ip_is_public(literal):
            raise SecurityError("non-public IP address blocked")
        return url
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith((".local", ".internal", ".localhost")):
        raise SecurityError("internal hostname blocked")
    if resolve:
        try:
            infos = resolver(host, port or (443 if parts.scheme == "https" else 80))
        except OSError as exc:
            raise SecurityError(f"cannot resolve host: {host}") from exc
        if not infos:
            raise SecurityError("host resolved to nothing")
        for info in infos:
            addr = info[4][0].split("%")[0]
            if not _ip_is_public(ipaddress.ip_address(addr)):
                raise SecurityError("host resolves to non-public address")
    return url


def check_redirect(origin: str, target: str, allowlist: Optional[Iterable[str]] = None, **kwargs) -> str:
    """Validate a redirect hop: same rules as endpoints, and no HTTPS->HTTP downgrade."""
    if urlsplit(origin).scheme == "https" and urlsplit(target).scheme != "https":
        raise SecurityError("redirect downgrades from https")
    return validate_endpoint(target, allowlist=allowlist, **kwargs)


def enforce_size(data: bytes, limit: int, what: str = "payload") -> bytes:
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if len(data) > limit:
        raise SecurityError(f"{what} exceeds {limit} bytes")
    return data
