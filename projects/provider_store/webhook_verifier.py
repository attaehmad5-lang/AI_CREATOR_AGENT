"""Generic HMAC-SHA256 webhook verifier with freshness and replay protection.

Provider-specific signature semantics must be configured from authoritative docs.
Secrets are resolved from environment-variable references and never returned/logged.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from datetime import datetime, timezone
from threading import RLock
from typing import Any


class ProviderWebhookVerifier:
    HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,80}")
    ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,79}")

    def __init__(
        self,
        provider_configs: dict[str, dict[str, Any]],
        *,
        environ: dict[str, str] | None = None,
        tolerance_seconds: int = 300,
        max_body_bytes: int = 1_000_000,
        max_replay_entries: int = 10000,
    ) -> None:
        if not isinstance(provider_configs, dict):
            raise ValueError("provider_configs must be a mapping")
        for name, value, minimum, maximum in (
            ("tolerance_seconds", tolerance_seconds, 1, 86400),
            ("max_body_bytes", max_body_bytes, 1, 100_000_000),
            ("max_replay_entries", max_replay_entries, 1, 1_000_000),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside the supported range")
        if environ is not None and (
            not isinstance(environ, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environ.items())
        ):
            raise ValueError("environ must map strings to strings")
        self.provider_configs: dict[str, dict[str, str]] = {}
        for provider_id, config in provider_configs.items():
            if not isinstance(provider_id, str) or not provider_id.strip() or len(provider_id) > 128:
                raise ValueError("invalid provider ID")
            if not isinstance(config, dict):
                raise ValueError("provider webhook config must be an object")
            signature_header = config.get("signature_header", "X-Webhook-Signature")
            timestamp_header = config.get("timestamp_header", "X-Webhook-Timestamp")
            event_id_header = config.get("event_id_header", "X-Webhook-Event-ID")
            secret_env = config.get("secret_env")
            prefix = config.get("signature_prefix", "sha256=")
            if any(
                not isinstance(header, str) or not self.HEADER_NAME.fullmatch(header)
                for header in (signature_header, timestamp_header, event_id_header)
            ):
                raise ValueError("invalid webhook header name")
            if len({signature_header.casefold(), timestamp_header.casefold(), event_id_header.casefold()}) != 3:
                raise ValueError("webhook header names must be distinct")
            if not isinstance(secret_env, str) or not self.ENV_NAME.fullmatch(secret_env):
                raise ValueError("secret_env must be an uppercase environment-variable reference")
            if not isinstance(prefix, str) or prefix not in {"", "sha256="}:
                raise ValueError("unsupported webhook signature prefix")
            self.provider_configs[provider_id] = {
                "signature_header": signature_header,
                "timestamp_header": timestamp_header,
                "event_id_header": event_id_header,
                "secret_env": secret_env,
                "signature_prefix": prefix,
            }
        self.environ = os.environ if environ is None else dict(environ)
        self.tolerance_seconds = tolerance_seconds
        self.max_body_bytes = max_body_bytes
        self.max_replay_entries = max_replay_entries
        self._lock = RLock()
        self._seen_events: dict[str, float] = {}

    @staticmethod
    def _now(value: datetime | None) -> float:
        current = datetime.now(timezone.utc) if value is None else value
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return current.timestamp()

    @staticmethod
    def _safe_id(value: Any, *, maximum: int = 256) -> bool:
        return (
            isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and value == value.strip() and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )

    def verify(
        self,
        provider_id: str,
        *,
        headers: dict[str, str],
        body: bytes,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not provider_id.strip():
            return {"status": "INVALID_PROVIDER", "verified": False}
        config = self.provider_configs.get(provider_id)
        if config is None:
            return {"status": "WEBHOOK_PROVIDER_NOT_CONFIGURED", "verified": False, "provider": provider_id}
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            return {"status": "INVALID_HEADERS", "verified": False, "provider": provider_id}
        if not isinstance(body, bytes):
            return {"status": "INVALID_BODY", "verified": False, "provider": provider_id}
        if len(body) > self.max_body_bytes:
            return {"status": "WEBHOOK_BODY_TOO_LARGE", "verified": False, "provider": provider_id}
        try:
            current = self._now(now)
        except ValueError:
            return {"status": "INVALID_CURRENT_TIME", "verified": False, "provider": provider_id}

        normalized: dict[str, str] = {}
        for key, value in headers.items():
            lowered = key.casefold()
            if lowered in normalized:
                return {"status": "DUPLICATE_HEADER", "verified": False, "provider": provider_id}
            normalized[lowered] = value
        signature_text = normalized.get(config["signature_header"].casefold())
        timestamp_text = normalized.get(config["timestamp_header"].casefold())
        event_id = normalized.get(config["event_id_header"].casefold())
        if not isinstance(signature_text, str) or not isinstance(timestamp_text, str) or not self._safe_id(event_id):
            return {"status": "WEBHOOK_HEADERS_MISSING", "verified": False, "provider": provider_id}
        if not re.fullmatch(r"[0-9]{1,12}", timestamp_text):
            return {"status": "INVALID_WEBHOOK_TIMESTAMP", "verified": False, "provider": provider_id}
        timestamp = int(timestamp_text)
        if abs(current - timestamp) > self.tolerance_seconds:
            return {"status": "WEBHOOK_TIMESTAMP_OUT_OF_WINDOW", "verified": False, "provider": provider_id}
        secret = self.environ.get(config["secret_env"])
        if not isinstance(secret, str) or len(secret) < 16 or len(secret) > 16384:
            return {"status": "WEBHOOK_SECRET_UNAVAILABLE", "verified": False, "provider": provider_id}
        signature_hex = signature_text
        prefix = config["signature_prefix"]
        if prefix:
            if not signature_hex.startswith(prefix):
                return {"status": "INVALID_WEBHOOK_SIGNATURE_FORMAT", "verified": False, "provider": provider_id}
            signature_hex = signature_hex[len(prefix):]
        if not re.fullmatch(r"[A-Fa-f0-9]{64}", signature_hex):
            return {"status": "INVALID_WEBHOOK_SIGNATURE_FORMAT", "verified": False, "provider": provider_id}

        signed = timestamp_text.encode("ascii") + b"." + body
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature_hex.casefold()):
            return {"status": "WEBHOOK_SIGNATURE_INVALID", "verified": False, "provider": provider_id}

        event_key = hashlib.sha256((provider_id + "\0" + event_id).encode("utf-8")).hexdigest()
        with self._lock:
            for key, expires_at in list(self._seen_events.items()):
                if expires_at < current:
                    del self._seen_events[key]
            if event_key in self._seen_events:
                return {"status": "WEBHOOK_REPLAY_DETECTED", "verified": False, "provider": provider_id}
            if len(self._seen_events) >= self.max_replay_entries:
                return {"status": "WEBHOOK_REPLAY_CACHE_FULL", "verified": False, "provider": provider_id}
            self._seen_events[event_key] = current + self.tolerance_seconds
        return {"status": "WEBHOOK_VERIFIED", "verified": True, "provider": provider_id}
