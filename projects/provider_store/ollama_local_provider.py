"""Loopback-only Ollama adapter for local/self-hosted text models.

The adapter deliberately permits only localhost/loopback HTTP endpoints. It
never follows redirects, contacts public hosts, or falls back to a cloud API.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import threading
from typing import Any
from urllib.parse import urlsplit

_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class OllamaLocalProvider:
    """Minimal safe local Ollama model discovery and non-streaming generation."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:11434",
        *,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 2_000_000,
        max_prompt_chars: int = 200_000,
        max_output_chars: int = 1_000_000,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 512:
            raise ValueError("endpoint must be a bounded URL")
        try:
            parsed = urlsplit(endpoint)
            host = parsed.hostname
            port = parsed.port or 11434
        except ValueError as exc:
            raise ValueError("invalid local model endpoint") from exc
        if (
            parsed.scheme != "http"
            or host is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not 1 <= port <= 65535
        ):
            raise ValueError("endpoint must be a plain loopback HTTP URL")
        normalized_host = host.rstrip(".").lower()
        try:
            loopback = ipaddress.ip_address(normalized_host).is_loopback
        except ValueError:
            loopback = normalized_host == "localhost"
        if not loopback:
            raise ValueError("only localhost/loopback model endpoints are permitted")

        timeout = self._finite_range(timeout_seconds, minimum=0.1, maximum=120.0)
        if timeout is None:
            raise ValueError("timeout_seconds is outside safe limits")
        for value, name, minimum, maximum in (
            (max_response_bytes, "max_response_bytes", 1024, 10_000_000),
            (max_prompt_chars, "max_prompt_chars", 1, 1_000_000),
            (max_output_chars, "max_output_chars", 1, 5_000_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside safe limits")

        self.endpoint = f"http://{normalized_host}:{port}"
        self.host = normalized_host
        self.port = port
        self.timeout_seconds = timeout
        self.max_response_bytes = max_response_bytes
        self.max_prompt_chars = max_prompt_chars
        self.max_output_chars = max_output_chars
        self._lock = threading.RLock()
        self._available_models: set[str] = set()

    @staticmethod
    def _finite_range(value: Any, *, minimum: float, maximum: float) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except (ValueError, TypeError, OverflowError):
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return number if minimum <= number <= maximum else None

    @staticmethod
    def _model_name(value: Any) -> bool:
        return isinstance(value, str) and _MODEL_RE.fullmatch(value) is not None

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=self.timeout_seconds
        )
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read(self.max_response_bytes + 1)
            status = response.status
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            return {
                "status": "LOCAL_CONNECTION_FAILED",
                "error_type": type(exc).__name__,
                "data": None,
            }
        finally:
            connection.close()

        if len(raw) > self.max_response_bytes:
            return {"status": "LOCAL_RESPONSE_TOO_LARGE", "data": None}
        if status != 200:
            return {
                "status": "LOCAL_HTTP_ERROR",
                "http_status": status,
                "data": None,
            }
        if content_type != "application/json":
            return {"status": "INVALID_LOCAL_RESPONSE", "data": None}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"status": "INVALID_LOCAL_RESPONSE", "data": None}
        if not isinstance(data, dict):
            return {"status": "INVALID_LOCAL_RESPONSE", "data": None}
        return {"status": "LOCAL_RESPONSE_AVAILABLE", "data": data}

    def discover_models(self) -> dict[str, Any]:
        """Fetch and validate the local /api/tags model list."""
        result = self._request_json("GET", "/api/tags")
        if result["status"] != "LOCAL_RESPONSE_AVAILABLE":
            return {key: value for key, value in result.items() if key != "data"} | {
                "models": None
            }
        data = result["data"]
        models = data.get("models")
        if not isinstance(models, list) or len(models) > 1000:
            return {"status": "INVALID_LOCAL_MODEL_CATALOG", "models": None}
        names: list[str] = []
        for model in models:
            if not isinstance(model, dict) or not self._model_name(model.get("name")):
                return {"status": "INVALID_LOCAL_MODEL_CATALOG", "models": None}
            names.append(model["name"])
        if len(set(names)) != len(names):
            return {"status": "INVALID_LOCAL_MODEL_CATALOG", "models": None}
        with self._lock:
            self._available_models = set(names)
        return {
            "status": "LOCAL_MODELS_AVAILABLE" if names else "NO_LOCAL_MODELS",
            "model_count": len(names),
            "models": names,
        }

    def health_check(self) -> dict[str, Any]:
        """Report only actual local model discovery results."""
        result = self.discover_models()
        return {
            "status": result["status"],
            "healthy": result["status"] == "LOCAL_MODELS_AVAILABLE",
            "model_count": result.get("model_count", 0),
            "models": result.get("models"),
        }

    def generate_text(self, *, model: str, prompt: str) -> dict[str, Any]:
        """Generate text using a previously discovered local model."""
        if not self._model_name(model):
            return {"status": "INVALID_MODEL", "text": None}
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > self.max_prompt_chars:
            return {"status": "INVALID_PROMPT", "text": None}
        with self._lock:
            known = model in self._available_models
        if not known:
            return {"status": "MODEL_NOT_DISCOVERED", "text": None}

        result = self._request_json(
            "POST",
            "/api/generate",
            {"model": model, "prompt": prompt, "stream": False},
        )
        if result["status"] != "LOCAL_RESPONSE_AVAILABLE":
            return {key: value for key, value in result.items() if key != "data"} | {
                "text": None
            }
        data = result["data"]
        text = data.get("response")
        if (
            not isinstance(text, str)
            or len(text) > self.max_output_chars
            or data.get("done") is not True
        ):
            return {"status": "INVALID_LOCAL_GENERATION_RESPONSE", "text": None}
        return {
            "status": "LOCAL_GENERATION_COMPLETED",
            "model": model,
            "text": text,
            "provider_call_external": False,
        }
