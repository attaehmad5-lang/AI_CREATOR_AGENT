"""Bounded HTTPS transport with redirect revalidation and connect-time DNS pinning.

This transport intentionally avoids proxy support and automatic redirects. Every
redirect is resolved and validated again, then the TCP connection is pinned to
one validated public IP while TLS certificate verification/SNI use the URL host.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlsplit

from .errors import SecurityError
from .security import check_redirect, validate_endpoint, _ip_is_public

Resolver = Callable[..., list]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, pinned_ip: str, timeout: float, context: ssl.SSLContext):
        super().__init__(host=host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._original_host = host

    def connect(self) -> None:
        if self._tunnel_host:
            raise SecurityError("HTTP CONNECT tunneling is disabled")
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self._original_host)
        except BaseException:
            raw.close()
            raise


class SecureHTTPTransport:
    """Synchronous HTTPS-only client with strict request/response bounds."""

    REDIRECT_STATUSES = {301, 302, 303, 307, 308}
    ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}

    def __init__(
        self,
        *,
        allowlist: Optional[Iterable[str]] = None,
        timeout: float = 10.0,
        max_request_bytes: int = 2 * 1024 * 1024,
        max_response_bytes: int = 10 * 1024 * 1024,
        max_redirects: int = 3,
        resolver: Resolver = socket.getaddrinfo,
        connection_factory=None,
        ssl_context: Optional[ssl.SSLContext] = None,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if max_request_bytes < 0 or max_response_bytes < 0:
            raise ValueError("body limits must be >= 0")
        if max_redirects < 0 or max_redirects > 10:
            raise ValueError("max_redirects must be between 0 and 10")
        self.allowlist = list(allowlist) if allowlist is not None else None
        self.timeout = timeout
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.resolver = resolver
        self.connection_factory = connection_factory or _PinnedHTTPSConnection
        self.ssl_context = ssl_context or ssl.create_default_context()

    def _resolve_public_ips(self, host: str, port: int) -> list[str]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if not _ip_is_public(literal):
                raise SecurityError("non-public IP address blocked")
            return [str(literal)]
        if host.lower() == "localhost" or host.lower().endswith((".local", ".internal", ".localhost")):
            raise SecurityError("internal hostname blocked")
        try:
            infos = self.resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise SecurityError("endpoint DNS resolution failed") from exc
        if not infos:
            raise SecurityError("endpoint resolved to no addresses")
        ips: list[str] = []
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0].split("%")[0])
            except (ValueError, IndexError, TypeError) as exc:
                raise SecurityError("endpoint returned an invalid DNS address") from exc
            if not _ip_is_public(address):
                raise SecurityError("endpoint resolves to a non-public address")
            normalized = str(address)
            if normalized not in ips:
                ips.append(normalized)
        return ips

    @staticmethod
    def _validate_headers(headers: Optional[dict]) -> dict[str, str]:
        if headers is None:
            return {}
        if not isinstance(headers, dict):
            raise SecurityError("headers must be a mapping")
        clean = {}
        forbidden = {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization"}
        for key, value in headers.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise SecurityError("header names and values must be strings")
            if any(ch in key or ch in value for ch in ("\r", "\n")):
                raise SecurityError("newline in HTTP header")
            if key.casefold() in forbidden:
                raise SecurityError(f"caller may not set the {key} header")
            clean[key] = value
        return clean

    def request(self, method: str, url: str, *, headers: Optional[dict] = None, body: bytes | None = None) -> dict:
        method = method.upper() if isinstance(method, str) else ""
        if method not in self.ALLOWED_METHODS:
            raise SecurityError("HTTP method is not allowed")
        if body is not None and not isinstance(body, bytes):
            raise SecurityError("request body must be bytes")
        body = body or b""
        if len(body) > self.max_request_bytes:
            raise SecurityError("request body exceeds configured limit")
        safe_headers = self._validate_headers(headers)
        safe_headers["Connection"] = "close"
        if body:
            safe_headers["Content-Length"] = str(len(body))
        current_url = url
        redirects = 0
        current_method = method
        current_body = body
        while True:
            validate_endpoint(current_url, allowlist=self.allowlist, resolve=False)
            parts = urlsplit(current_url)
            host = parts.hostname
            if not host:
                raise SecurityError("missing endpoint host")
            port = parts.port or 443
            pinned_ip = self._resolve_public_ips(host, port)[0]
            connection = self.connection_factory(host, port, pinned_ip, self.timeout, self.ssl_context)
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            try:
                connection.request(current_method, path, body=current_body or None, headers=safe_headers)
                response = connection.getresponse()
                data = response.read(self.max_response_bytes + 1)
                if len(data) > self.max_response_bytes:
                    raise SecurityError("response body exceeds configured limit")
                status = int(response.status)
                response_headers = {str(k).lower(): str(v) for k, v in response.getheaders()}
                location = response_headers.get("location")
            finally:
                connection.close()
            if status not in self.REDIRECT_STATUSES or not location:
                return {
                    "status": status,
                    "headers": response_headers,
                    "body": data,
                    "url": current_url,
                    "redirects": redirects,
                }
            if redirects >= self.max_redirects:
                raise SecurityError("redirect limit exceeded")
            target = urljoin(current_url, location)
            check_redirect(current_url, target, allowlist=self.allowlist, resolve=False)
            redirects += 1
            if status == 303 or (status in {301, 302} and current_method == "POST"):
                current_method, current_body = "GET", b""
                safe_headers.pop("Content-Length", None)
            current_url = target
