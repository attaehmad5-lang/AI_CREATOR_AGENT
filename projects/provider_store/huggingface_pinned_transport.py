"""DNS-pinned outbound transport for the Hugging Face Hub HTTP client.

The Hub's supported set_client_factory API is used to ensure InferenceClient
requests pass through the same pinned HTTPS transport as the other provider
adapters. Installation is process-wide for huggingface_hub's shared sync client.
"""
from __future__ import annotations

import math
import threading
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request

from .http_probe import ProviderPinnedHTTPSOpener

_ALLOWED_HOSTS = frozenset({
    "huggingface.co",
    "router.huggingface.co",
    "api-inference.huggingface.co",
})
_INSTALL_LOCK = threading.RLock()
_INSTALLED = False


def build_pinned_httpx_transport(
    httpx_module: Any,
    *,
    opener_factory: Callable[[], Any] = ProviderPinnedHTTPSOpener,
    max_request_bytes: int = 8 * 1024 * 1024,
    max_response_bytes: int = 64 * 1024 * 1024,
    timeout_seconds: float = 180.0,
) -> type:
    """Build an HTTPX BaseTransport without importing optional Hub dependencies."""
    if not callable(getattr(httpx_module, "BaseTransport", None)):
        raise RuntimeError("Hugging Face HTTP client has no supported BaseTransport")
    if not callable(getattr(httpx_module, "Response", None)):
        raise RuntimeError("Hugging Face HTTP client has no supported Response class")
    for value, name, minimum, maximum in (
        (max_request_bytes, "max_request_bytes", 1, 64 * 1024 * 1024),
        (max_response_bytes, "max_response_bytes", 1024, 128 * 1024 * 1024),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} is outside safe bounds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 0.1 <= float(timeout_seconds) <= 300
    ):
        raise ValueError("timeout_seconds is outside safe bounds")

    class PinnedHuggingFaceTransport(httpx_module.BaseTransport):
        """Synchronous HTTPX transport that delegates to the shared pinned opener."""

        def __init__(self) -> None:
            self._opener = opener_factory()

        @staticmethod
        def _request_url(request: Any) -> str:
            raw_url = str(getattr(request, "url", ""))
            try:
                parsed = urlsplit(raw_url)
                port = parsed.port
            except (TypeError, ValueError):
                raise ValueError("HUGGINGFACE_REQUEST_URL_INVALID") from None
            host = parsed.hostname.casefold().rstrip(".") if parsed.hostname else ""
            try:
                query_fields = parse_qsl(
                    parsed.query, keep_blank_values=True, max_num_fields=64,
                )
            except ValueError:
                raise ValueError("HUGGINGFACE_REQUEST_QUERY_INVALID") from None
            sensitive_query_markers = (
                "token", "api_key", "apikey", "password", "secret",
                "authorization", "bearer", "credential",
            )
            if any(
                any(marker in key.casefold().replace("-", "_") for marker in sensitive_query_markers)
                for key, _value in query_fields
            ):
                raise ValueError("HUGGINGFACE_CREDENTIAL_IN_QUERY_REJECTED")
            if (
                parsed.scheme != "https"
                or host not in _ALLOWED_HOSTS
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or port not in (None, 443)
                or len(raw_url) > 8192
            ):
                raise ValueError("HUGGINGFACE_REQUEST_HOST_NOT_ALLOWED")
            return raw_url

        def handle_request(self, request: Any) -> Any:
            url = self._request_url(request)
            method = getattr(request, "method", None)
            if not isinstance(method, str) or not method.isalpha() or len(method) > 12:
                raise ValueError("HUGGINGFACE_REQUEST_METHOD_INVALID")

            # Bound request body accumulation before passing bytes to urllib.
            chunks: list[bytes] = []
            total = 0
            stream = getattr(request, "stream", None)
            try:
                iterator = iter(stream) if stream is not None else iter(())
                for chunk in iterator:
                    if not isinstance(chunk, bytes):
                        raise ValueError("HUGGINGFACE_REQUEST_BODY_INVALID")
                    total += len(chunk)
                    if total > max_request_bytes:
                        raise ValueError("HUGGINGFACE_REQUEST_BODY_TOO_LARGE")
                    chunks.append(chunk)
            except ValueError:
                raise
            except Exception:
                raise ValueError("HUGGINGFACE_REQUEST_BODY_READ_FAILED") from None
            body = b"".join(chunks) if chunks else None

            raw_headers = getattr(request, "headers", None)
            if raw_headers is None or not callable(getattr(raw_headers, "items", None)):
                raise ValueError("HUGGINGFACE_REQUEST_HEADERS_INVALID")
            headers: dict[str, str] = {}
            forbidden = {
                "host", "content-length", "transfer-encoding", "connection",
                "keep-alive", "proxy-authorization", "proxy-authenticate",
                "te", "trailer", "upgrade", "accept-encoding",
            }
            for raw_name, raw_value in raw_headers.items():
                name = str(raw_name)
                value = str(raw_value)
                if (
                    not name or len(name) > 128
                    or any(not (ch.isalnum() or ch == "-") for ch in name)
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
                ):
                    raise ValueError("HUGGINGFACE_REQUEST_HEADER_INVALID")
                if name.casefold() not in forbidden:
                    headers[name] = value
            # Avoid decompression ambiguity: the adapter enforces a hard response
            # byte cap on the bytes actually read from the provider.
            headers["Accept-Encoding"] = "identity"
            request_object = Request(url, data=body, headers=headers, method=method)

            timeout = float(timeout_seconds)
            extensions = getattr(request, "extensions", {})
            timeout_config = extensions.get("timeout") if isinstance(extensions, dict) else None
            if isinstance(timeout_config, dict):
                candidates = [
                    value for value in timeout_config.values()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(float(value)) and float(value) > 0
                ]
                if candidates:
                    timeout = min(timeout, *(float(value) for value in candidates))

            response = None
            try:
                response = self._opener.open(request_object, timeout=timeout)
                status = response.getcode()
                if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
                    raise ValueError("HUGGINGFACE_RESPONSE_STATUS_INVALID")
                if 300 <= status < 400:
                    raise ValueError("HUGGINGFACE_REDIRECT_REJECTED")
                response_headers = getattr(response, "headers", None)
                if response_headers is not None:
                    length = response_headers.get("Content-Length")
                    if length is not None:
                        if not isinstance(length, str) or not length.isdigit() or int(length) > max_response_bytes:
                            raise ValueError("HUGGINGFACE_RESPONSE_TOO_LARGE")
                payload = response.read(max_response_bytes + 1)
                if not isinstance(payload, (bytes, bytearray)) or len(payload) > max_response_bytes:
                    raise ValueError("HUGGINGFACE_RESPONSE_TOO_LARGE")
                headers_out = dict(response_headers.items()) if response_headers is not None else {}
                return httpx_module.Response(
                    status_code=status,
                    headers=headers_out,
                    content=bytes(payload),
                    request=request,
                )
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass

        def close(self) -> None:
            # The pinned opener has no persistent connection pool or socket to close.
            return None

    return PinnedHuggingFaceTransport


def install_huggingface_pinned_transport(
    *,
    httpx_module: Any | None = None,
    set_client_factory: Callable[[Callable[[], Any]], Any] | None = None,
    timeout_seconds: float = 180.0,
) -> None:
    """Install the pinned transport through Hugging Face's supported client factory.

    The sync client factory is process-global in huggingface_hub. Installation is
    serialized and occurs once; unsupported client-library versions fail closed.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        if httpx_module is None or set_client_factory is None:
            try:
                import huggingface_hub
                from huggingface_hub.utils import httpx as hub_httpx
            except ImportError:
                raise RuntimeError("a supported huggingface_hub HTTP client is required") from None
            httpx_module = hub_httpx
            setter = getattr(huggingface_hub, "set_client_factory", None)
            if not callable(setter):
                try:
                    from huggingface_hub.utils._http import set_client_factory as setter
                except ImportError:
                    setter = None
            set_client_factory = setter
        if not callable(set_client_factory):
            raise RuntimeError("huggingface_hub does not expose set_client_factory")
        transport_class = build_pinned_httpx_transport(
            httpx_module, timeout_seconds=timeout_seconds,
        )

        def factory() -> Any:
            transport = transport_class()
            try:
                return httpx_module.Client(
                    transport=transport,
                    follow_redirects=False,
                    timeout=float(timeout_seconds),
                )
            except Exception:
                transport.close()
                raise

        set_client_factory(factory)
        _INSTALLED = True
