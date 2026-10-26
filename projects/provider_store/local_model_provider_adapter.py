"""Local Ollama-compatible text model adapter with loopback-only network policy."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class LocalModelProviderAdapter:
    """Call a local Ollama /api/generate endpoint; never sends prompts to remote hosts."""

    def __init__(
        self,
        *,
        endpoint: str = "http://127.0.0.1:11434/api/generate",
        model_id: str,
        timeout_seconds: float = 30.0,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        self.endpoint = self._validate_endpoint(endpoint)
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
            raise ValueError("invalid local model_id")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1 <= timeout_seconds <= 120
        ):
            raise ValueError("timeout_seconds must be between 1 and 120")
        if transport is not None and not callable(transport):
            raise ValueError("transport must be callable")
        self.model_id = model_id
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        if not isinstance(endpoint, str) or len(endpoint) > 2048:
            raise ValueError("invalid local endpoint")
        try:
            parsed = urlsplit(endpoint)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid local endpoint") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/api/generate"
        ):
            raise ValueError("endpoint must be a local /api/generate URL without credentials/query/fragment")
        normalized_host = host.rstrip(".").lower()
        is_loopback = normalized_host == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(normalized_host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise ValueError("only localhost/loopback endpoints are allowed")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid endpoint port")
        return endpoint.rstrip("/")

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.transport is not None:
            response = self.transport(
                self.endpoint,
                dict(payload),
                timeout_seconds=self.timeout_seconds,
            )
            if not isinstance(response, dict):
                raise ValueError("transport returned invalid response")
            # Apply the same response-size bound to injected transports as to the
            # real HTTP transport; otherwise a custom transport can bypass the
            # resource limit and hand downstream code an unbounded payload.
            try:
                encoded_response = json.dumps(
                    response, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError) as exc:
                raise ValueError("transport returned a non-JSON response") from exc
            if len(encoded_response) > _MAX_RESPONSE_BYTES:
                raise ValueError("local model response exceeds size limit")
            return response
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"local model HTTP status {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("local model endpoint unavailable") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ValueError("local model response exceeds size limit")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("local model returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("local model response must be an object")
        return parsed

    def execute(
        self,
        *,
        prompt: str,
        system: str | None = None,
        provider_id: str | None = None,
        request_id: str | None = None,
        output_path: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Adapt local text generation to ProviderService's started/success contract."""
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
            return {"started": False, "success": False, "status": "INVALID_PROMPT"}
        if system is not None and (not isinstance(system, str) or len(system) > 20_000):
            return {"started": False, "success": False, "status": "INVALID_SYSTEM_PROMPT"}
        if not isinstance(output_path, (str, os.PathLike)) or not str(output_path).strip():
            return {"started": False, "success": False, "status": "OUTPUT_PATH_REQUIRED"}
        target = Path(output_path)
        if not target.parent.exists() or not target.parent.is_dir():
            return {"started": False, "success": False, "status": "OUTPUT_DIRECTORY_UNAVAILABLE"}
        result = self.generate_text(prompt, system=system)
        if result.get("status") in {"INVALID_PROMPT", "INVALID_SYSTEM_PROMPT"}:
            return {"started": False, "success": False, "status": result["status"]}
        if result.get("generated") is not True:
            return {
                "started": True,
                "success": False,
                "status": result.get("status"),
                "provider_id": provider_id or "local-ollama",
                "request_id": request_id,
                "result": result,
                "charge_outcome": "not_incurred",
                "actual_cost": 0.0,
            }
        fd, temporary_path = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(result["text"])
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
        except Exception as exc:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            return {
                "started": True,
                "success": False,
                "status": "LOCAL_OUTPUT_WRITE_FAILED",
                "error_type": type(exc).__name__,
                "provider_id": provider_id or "local-ollama",
                "request_id": request_id,
                "charge_outcome": "not_incurred",
                "actual_cost": 0.0,
            }
        return {
            "started": True,
            "success": True,
            "status": result.get("status"),
            "provider_id": provider_id or "local-ollama",
            "request_id": request_id,
            "output_path": str(target),
            "result": result,
            "text": result.get("text"),
            "actual_cost": 0.0,
            "charge_outcome": "not_incurred",
        }

    def generate_text(self, prompt: str, *, system: str | None = None) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100_000:
            return {"status": "INVALID_PROMPT", "generated": False}
        if system is not None and (
            not isinstance(system, str) or len(system) > 20_000
        ):
            return {"status": "INVALID_SYSTEM_PROMPT", "generated": False}
        payload: dict[str, Any] = {
            "model": self.model_id,
            "prompt": prompt,
            "stream": False,
        }
        if system is not None:
            payload["system"] = system
        try:
            response = self._request(payload)
        except Exception as exc:
            return {
                "status": "LOCAL_MODEL_REQUEST_FAILED",
                "generated": False,
                "error_type": type(exc).__name__,
            }
        text = response.get("response")
        # Ollama's non-streaming API marks completion with done=true. A response
        # without that explicit terminal marker may be truncated or incomplete;
        # never advertise it as a successful generation.
        if not isinstance(text, str) or not text.strip() or response.get("done") is not True:
            return {"status": "INVALID_LOCAL_MODEL_RESPONSE", "generated": False}
        return {
            "status": "LOCAL_TEXT_GENERATED",
            "generated": True,
            "provider_id": "local-ollama",
            "model_id": self.model_id,
            "text": text,
            "done": response.get("done") is True,
        }
