"""DNS-pinned HTTPS transport for provider connectivity probes.

The hostname remains the HTTP Host and TLS SNI identity while TCP connects to
one IP address validated during the immediately preceding DNS preflight.
"""
from __future__ import annotations

import socket
from http.client import HTTPSConnection
from urllib.request import HTTPSHandler, ProxyHandler, build_opener


class PinnedHTTPSConnection(HTTPSConnection):
    """HTTPS connection whose TCP destination is a prevalidated IP address."""

    def __init__(self, host: str, pinned_ip: str, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        if self._tunnel_host:
            raise OSError("proxy tunneling is disabled for pinned provider probes")
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket, server_hostname=self.host
            )
        except Exception:
            raw_socket.close()
            raise


class PinnedHTTPSHandler(HTTPSHandler):
    """urllib handler that preserves hostname verification with a pinned IP."""

    def __init__(self, pinned_ip: str):
        super().__init__()
        if not isinstance(pinned_ip, str) or not pinned_ip:
            raise ValueError("a validated pinned IP address is required")
        self.pinned_ip = pinned_ip

    def https_open(self, request):
        return self.do_open(
            lambda host, **kwargs: PinnedHTTPSConnection(
                host, self.pinned_ip, **kwargs
            ),
            request,
            context=self._context,
            check_hostname=self._check_hostname,
        )


def build_pinned_https_opener(pinned_ip: str, redirect_handler):
    """Build a proxy-free opener that cannot re-resolve the target hostname."""
    return build_opener(
        ProxyHandler({}),
        redirect_handler,
        PinnedHTTPSHandler(pinned_ip),
    )
