"""Environment-backed credential resolution with in-memory refresh and no disk persistence."""
from __future__ import annotations

import math
import os
import threading
import time
from typing import Callable, Mapping, Optional

from .errors import CredentialExpiredError, CredentialUnavailableError, ValidationError

RefreshCallback = Callable[[str, str], Mapping[str, object]]


class CredentialManager:
    """Resolve credentials from environment references; never persist raw secrets."""

    def __init__(
        self,
        *,
        environ: Optional[Mapping[str, str]] = None,
        clock: Callable[[], float] = time.time,
        refresh_before_seconds: float = 60.0,
    ):
        if (isinstance(refresh_before_seconds, bool)
                or not isinstance(refresh_before_seconds, (int, float))
                or not math.isfinite(refresh_before_seconds) or refresh_before_seconds < 0):
            raise ValueError("refresh_before_seconds must be finite and >= 0")
        self._environ = os.environ if environ is None else environ
        self._clock = clock
        self._refresh_before = float(refresh_before_seconds)
        self._refreshers: dict[str, RefreshCallback] = {}
        self._cache: dict[str, tuple[str, Optional[float]]] = {}
        self._lock = threading.RLock()

    def register_refresher(self, provider_id: str, callback: RefreshCallback) -> None:
        if not isinstance(provider_id, str) or not provider_id.strip() or not callable(callback):
            raise ValidationError("provider_id and callable refresh callback are required")
        with self._lock:
            self._refreshers[provider_id] = callback

    def clear_cache(self, provider_id: Optional[str] = None) -> None:
        with self._lock:
            if provider_id is None:
                self._cache.clear()
            else:
                self._cache.pop(provider_id, None)

    @staticmethod
    def _expiry(value: Optional[str]) -> Optional[float]:
        if value is None or not value.strip():
            return None
        try:
            result = float(value)
        except ValueError as exc:
            raise ValidationError("credential expiry must be a Unix timestamp") from exc
        if not math.isfinite(result):
            raise ValidationError("credential expiry must be finite")
        return result

    def resolve(
        self,
        provider_id: str,
        env_var: str,
        *,
        expires_at_env: Optional[str] = None,
    ) -> str:
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValidationError("provider_id must be non-empty")
        if not isinstance(env_var, str) or not env_var.strip():
            raise ValidationError("env_var must be non-empty")
        now = self._clock()
        with self._lock:
            cached = self._cache.get(provider_id)
            if cached:
                token, expiry = cached
                if expiry is None or expiry > now + self._refresh_before:
                    return token
            token = self._environ.get(env_var)
            if not isinstance(token, str) or not token.strip():
                raise CredentialUnavailableError(f"credential reference unavailable for {provider_id}")
            expiry = self._expiry(self._environ.get(expires_at_env) if expires_at_env else None)
            if expiry is None or expiry > now + self._refresh_before:
                return token
            refresher = self._refreshers.get(provider_id)
            if refresher is None:
                raise CredentialExpiredError(f"credential expired or near expiry for {provider_id}")
            # Callback is injected by the host; network/OAuth policy is not embedded here.
            refreshed = refresher(provider_id, token)
            if not isinstance(refreshed, Mapping):
                raise CredentialExpiredError("credential refresh returned an invalid response")
            new_token = refreshed.get("token")
            new_expiry = refreshed.get("expires_at")
            if not isinstance(new_token, str) or not new_token.strip():
                raise CredentialExpiredError("credential refresh returned no token")
            if isinstance(new_expiry, bool) or not isinstance(new_expiry, (int, float)) or not math.isfinite(new_expiry):
                raise CredentialExpiredError("credential refresh returned an invalid expiry")
            if new_expiry <= now + self._refresh_before:
                raise CredentialExpiredError("refreshed credential is already expired or near expiry")
            self._cache[provider_id] = (new_token, float(new_expiry))
            return new_token
