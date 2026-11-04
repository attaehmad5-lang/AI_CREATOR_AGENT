"""OIDC/JWT identity verification for provider-store API boundaries.

The issuer and JWKS host are explicit deployment configuration. Tokens are
verified cryptographically with PyJWT; no unverified claim is trusted. The
returned AuthenticatedPrincipal is an in-process capability and must never be
constructed from a caller-supplied principal_id or serialized to a client.
"""
from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .provider_authorization import _PRINCIPAL_RE


class OIDCAuthenticationError(ValueError):
    """Stable, non-sensitive authentication failure code."""

    def __init__(self, status: str) -> None:
        self.status = status if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", status) else "OIDC_AUTHENTICATION_FAILED"
        super().__init__(self.status)


class AuthenticatedPrincipal(str):
    """Opaque in-process principal produced only after signature and claim checks."""

    def __new__(
        cls, principal_id: str, *, issuer: str, subject: str, marker: object
    ) -> "AuthenticatedPrincipal":
        value = str.__new__(cls, principal_id)
        value.issuer = issuer
        value.subject = subject
        value._authenticator_marker = marker
        return value


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class OIDCJWKSResolver:
    """Bounded HTTPS JWKS resolver with an exact hostname allowlist and cache."""

    def __init__(
        self,
        jwks_uri: str,
        *,
        allowed_hosts: set[str] | tuple[str, ...] | list[str],
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: int = 300,
        max_response_bytes: int = 1_048_576,
        opener: Any | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(jwks_uri) if isinstance(jwks_uri, str) else None
        hosts = {host.lower().rstrip(".") for host in allowed_hosts if isinstance(host, str)}
        if (
            parsed is None or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or parsed.hostname.lower().rstrip(".") not in hosts
            or not parsed.path.startswith("/")
        ):
            raise ValueError("JWKS URI must be HTTPS and match an explicitly allowed host")
        if (
            isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0.1 <= timeout_seconds <= 10
        ):
            raise ValueError("JWKS timeout must be within 0.1..10 seconds")
        if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, int) or not 30 <= cache_ttl_seconds <= 3600:
            raise ValueError("JWKS cache TTL must be within 30..3600 seconds")
        if isinstance(max_response_bytes, bool) or not isinstance(max_response_bytes, int) or not 1024 <= max_response_bytes <= 4_194_304:
            raise ValueError("JWKS response limit must be within 1KiB..4MiB")
        self.jwks_uri = jwks_uri
        self.timeout_seconds = float(timeout_seconds)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.max_response_bytes = max_response_bytes
        self._opener = opener or urllib.request.build_opener(
            _NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        self._lock = threading.RLock()
        self._keys: list[dict[str, Any]] = []
        self._expires_at = 0.0

    def _load_keys(self, *, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            now = time.monotonic()
            if not force and self._keys and now < self._expires_at:
                return self._keys
            request = urllib.request.Request(
                self.jwks_uri, headers={"Accept": "application/json"}, method="GET"
            )
            response = None
            try:
                response = self._opener.open(request, timeout=self.timeout_seconds)
                status = response.getcode()
                if status != 200:
                    raise OIDCAuthenticationError("OIDC_JWKS_HTTP_ERROR")
                body = response.read(self.max_response_bytes + 1)
                if not isinstance(body, bytes) or len(body) > self.max_response_bytes:
                    raise OIDCAuthenticationError("OIDC_JWKS_RESPONSE_TOO_LARGE")
                document = json.loads(body.decode("utf-8"))
            except OIDCAuthenticationError:
                raise
            except Exception as exc:
                # Do not expose URL, headers, response body, or transport exception text.
                status = "OIDC_JWKS_TIMEOUT" if isinstance(exc, TimeoutError) else "OIDC_JWKS_UNAVAILABLE"
                raise OIDCAuthenticationError(status) from None
            finally:
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
            if (
                not isinstance(document, dict) or not isinstance(document.get("keys"), list)
                or not 1 <= len(document["keys"]) <= 100
                or any(not isinstance(item, dict) for item in document["keys"])
            ):
                raise OIDCAuthenticationError("OIDC_JWKS_INVALID")
            seen: set[str] = set()
            for item in document["keys"]:
                kid = item.get("kid")
                if not isinstance(kid, str) or not 1 <= len(kid) <= 128 or kid in seen:
                    raise OIDCAuthenticationError("OIDC_JWKS_INVALID")
                seen.add(kid)
            self._keys = document["keys"]
            self._expires_at = now + self.cache_ttl_seconds
            return self._keys

    def __call__(self, token: str, header: dict[str, Any]) -> Any:
        kid = header.get("kid")
        algorithm = header.get("alg")
        if not isinstance(kid, str) or not kid or not isinstance(algorithm, str):
            raise OIDCAuthenticationError("OIDC_TOKEN_HEADER_INVALID")
        for attempt in range(2):
            keys = self._load_keys(force=attempt == 1)
            matches = [item for item in keys if item.get("kid") == kid]
            if len(matches) == 1:
                item = matches[0]
                if (
                    item.get("use", "sig") != "sig"
                    or ("key_ops" in item and (
                        not isinstance(item["key_ops"], list) or "verify" not in item["key_ops"]
                    ))
                    or ("alg" in item and item["alg"] != algorithm)
                ):
                    raise OIDCAuthenticationError("OIDC_SIGNING_KEY_INVALID")
                try:
                    import jwt
                    return jwt.PyJWK.from_dict(item, algorithm=algorithm).key
                except Exception:
                    raise OIDCAuthenticationError("OIDC_SIGNING_KEY_INVALID") from None
        raise OIDCAuthenticationError("OIDC_SIGNING_KEY_NOT_FOUND")


class OIDCIdentityAuthenticator:
    """Verify signed OIDC JWTs and issue opaque principals for service authorization."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | list[str] | tuple[str, ...],
        key_resolver: Callable[[str, dict[str, Any]], Any],
        allowed_algorithms: tuple[str, ...] = ("RS256",),
        clock_skew_seconds: int = 30,
        max_token_bytes: int = 16_384,
        max_token_age_seconds: int | None = 3600,
        require_mfa: bool = False,
        required_amr: tuple[str, ...] = ("mfa",),
        required_acr: tuple[str, ...] = (),
        principal_mapper: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(issuer) if isinstance(issuer, str) else None
        audiences = [audience] if isinstance(audience, str) else list(audience) if isinstance(audience, (list, tuple)) else []
        if (
            parsed is None or parsed.scheme != "https" or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        ):
            raise ValueError("OIDC issuer must be an HTTPS issuer URL")
        if not audiences or any(not isinstance(item, str) or not item or len(item) > 256 for item in audiences):
            raise ValueError("OIDC audience must contain non-empty identifiers")
        if not callable(key_resolver):
            raise ValueError("key_resolver must resolve trusted signing keys")
        if not isinstance(allowed_algorithms, tuple) or not allowed_algorithms or any(
            algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "EdDSA"}
            for algorithm in allowed_algorithms
        ):
            raise ValueError("unsupported or empty JWT algorithm allowlist")
        if len(set(allowed_algorithms)) != len(allowed_algorithms):
            raise ValueError("JWT algorithms must be unique")
        if isinstance(clock_skew_seconds, bool) or not isinstance(clock_skew_seconds, int) or not 0 <= clock_skew_seconds <= 120:
            raise ValueError("clock skew must be within 0..120 seconds")
        if isinstance(max_token_bytes, bool) or not isinstance(max_token_bytes, int) or not 1024 <= max_token_bytes <= 65_536:
            raise ValueError("token size limit must be within 1KiB..64KiB")
        if max_token_age_seconds is not None and (
            isinstance(max_token_age_seconds, bool) or not isinstance(max_token_age_seconds, int)
            or not 60 <= max_token_age_seconds <= 86_400
        ):
            raise ValueError("maximum token age must be within 60..86400 seconds")
        if not isinstance(require_mfa, bool):
            raise ValueError("require_mfa must be boolean")
        if not isinstance(required_amr, tuple) or not isinstance(required_acr, tuple):
            raise ValueError("MFA claim allowlists must be tuples")
        if any(not isinstance(item, str) or not item or len(item) > 128 for item in required_amr + required_acr):
            raise ValueError("MFA claim allowlists contain invalid values")
        if principal_mapper is not None and not callable(principal_mapper):
            raise ValueError("principal_mapper must be callable")
        self.issuer = issuer.rstrip("/")
        self.audiences = audiences
        self.key_resolver = key_resolver
        self.allowed_algorithms = allowed_algorithms
        self.clock_skew_seconds = clock_skew_seconds
        self.max_token_bytes = max_token_bytes
        self.max_token_age_seconds = max_token_age_seconds
        self.require_mfa = require_mfa
        self.required_amr = frozenset(required_amr)
        self.required_acr = frozenset(required_acr)
        self.principal_mapper = principal_mapper or (lambda claims: claims.get("sub"))
        self._marker = object()

    def authenticate(self, token: str) -> AuthenticatedPrincipal:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > self.max_token_bytes:
            raise OIDCAuthenticationError("OIDC_TOKEN_INVALID")
        try:
            import jwt
            header = jwt.get_unverified_header(token)
            if (
                not isinstance(header, dict)
                or header.get("alg") not in self.allowed_algorithms
                or not isinstance(header.get("kid"), str)
                or not header.get("kid")
                or any(name in header for name in ("jku", "jwk", "x5u", "crit"))
                or header.get("typ", "JWT") not in {"JWT", "at+jwt"}
            ):
                raise OIDCAuthenticationError("OIDC_TOKEN_HEADER_INVALID")
            key = self.key_resolver(token, header)
            if key is None:
                raise OIDCAuthenticationError("OIDC_SIGNING_KEY_UNAVAILABLE")
            claims = jwt.decode(
                token,
                key,
                algorithms=list(self.allowed_algorithms),
                audience=self.audiences,
                issuer=self.issuer,
                leeway=self.clock_skew_seconds,
                options={
                    "require": ["iss", "aud", "sub", "iat", "exp"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except OIDCAuthenticationError:
            raise
        except Exception as exc:
            error_name = type(exc).__name__
            status = "OIDC_SIGNING_KEY_UNAVAILABLE" if error_name in {
                "TimeoutError", "ConnectionError", "URLError"
            } else "OIDC_TOKEN_INVALID"
            raise OIDCAuthenticationError(status) from None

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise OIDCAuthenticationError("OIDC_SUBJECT_INVALID")
        now = time.time()
        issued_at = claims.get("iat")
        if (
            isinstance(issued_at, bool) or not isinstance(issued_at, (int, float))
            or issued_at > now + self.clock_skew_seconds
            or (self.max_token_age_seconds is not None and now - issued_at > self.max_token_age_seconds + self.clock_skew_seconds)
        ):
            raise OIDCAuthenticationError("OIDC_TOKEN_AGE_INVALID")
        if self.require_mfa:
            amr = claims.get("amr", [])
            if isinstance(amr, str):
                amr = [amr]
            acr = claims.get("acr")
            if (
                not isinstance(amr, list) or any(not isinstance(item, str) for item in amr)
                or not (
                    bool(self.required_acr and acr in self.required_acr)
                    or bool(self.required_amr.intersection(amr))
                )
            ):
                raise OIDCAuthenticationError("OIDC_MFA_REQUIRED")
        try:
            principal_id = self.principal_mapper(claims)
        except Exception:
            raise OIDCAuthenticationError("OIDC_PRINCIPAL_MAPPING_FAILED") from None
        if not isinstance(principal_id, str) or not _PRINCIPAL_RE.fullmatch(principal_id):
            raise OIDCAuthenticationError("OIDC_PRINCIPAL_MAPPING_INVALID")
        return AuthenticatedPrincipal(
            principal_id, issuer=self.issuer, subject=subject, marker=self._marker
        )

    def is_authenticated_principal(self, principal: Any) -> bool:
        return (
            isinstance(principal, AuthenticatedPrincipal)
            and getattr(principal, "_authenticator_marker", None) is self._marker
            and getattr(principal, "issuer", None) == self.issuer
        )
