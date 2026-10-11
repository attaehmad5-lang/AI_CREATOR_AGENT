"""Provider connection state management.

This module records provider state; it does not claim a live API connection
unless a separate provider-specific probe has actually succeeded.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_CATALOG = MODULE_DIR / "provider_catalog.json"
MASTER_CATALOG = MODULE_DIR / "provider_catalog_master.json"
DEFAULT_STATE = MODULE_DIR / "connection_state.json"


class ConnectionManager:
    VALID_STATES = {
        "LOCKED",
        "CONNECTING",
        "CONNECTED",
        "READY",
        "CREDENTIALS_PRESENT",
        "CREDENTIALS_MISSING",
        "CREDITS_UNAVAILABLE",
        "DISABLED",
        "ERROR",
        "UNKNOWN",
    }

    def __init__(
        self,
        catalog_path: str | os.PathLike[str] | None = None,
        state_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.catalog_path = Path(catalog_path) if catalog_path else self._default_catalog()
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE
        self.catalog = self._load_json(self.catalog_path)
        self.state = self._load_json(self.state_path, default={"version": "1.0", "policy": "USER_CONTROLLED", "providers": {}})
        self.state.setdefault("providers", {})

    @staticmethod
    def _default_catalog() -> Path:
        if DEFAULT_CATALOG.is_file():
            return DEFAULT_CATALOG
        if MASTER_CATALOG.is_file():
            return MASTER_CATALOG
        return DEFAULT_CATALOG

    @staticmethod
    def _load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.exists():
            if default is not None:
                return json.loads(json.dumps(default))
            raise FileNotFoundError(f"Required provider catalog not found: {path}")
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path.name}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object in {path.name}")
        return data

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            dir=str(self.state_path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.state, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.state_path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def list_providers(self) -> list[dict[str, Any]]:
        providers = self.catalog.get("providers", [])
        if not isinstance(providers, list):
            raise ValueError("Provider catalog 'providers' must be a list")
        return [item for item in providers if isinstance(item, dict)]

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return next((p for p in self.list_providers() if p.get("id") == provider_id), None)

    def provider_exists(self, provider_id: str) -> bool:
        return self.get_provider(provider_id) is not None

    def get_state(self, provider_id: str) -> dict[str, Any]:
        item = self.state["providers"].get(provider_id)
        if not isinstance(item, dict):
            return {"status": "UNKNOWN", "verified": False}
        # Return a copy so callers cannot mutate internal state without validation.
        return dict(item)

    def set_state(
        self,
        provider_id: str,
        status: str,
        *,
        verified: bool = False,
        detail: str | None = None,
    ) -> dict[str, Any]:
        if not self.provider_exists(provider_id):
            return {"status": "NOT_FOUND", "provider": provider_id}
        if status not in self.VALID_STATES:
            return {"status": "INVALID_STATE", "provider": provider_id}
        if status == "CONNECTED" and not verified:
            return {
                "status": "VERIFICATION_REQUIRED",
                "provider": provider_id,
                "saved": False,
            }

        record: dict[str, Any] = {
            "status": status,
            "verified": bool(verified),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if detail:
            # Do not store arbitrary provider exception text; it may contain secrets.
            record["detail"] = str(detail)[:200]
        self.state["providers"][provider_id] = record
        self._save()
        return {"provider": provider_id, **record, "saved": True}

    def credential_status(self, provider_id: str, environ: dict[str, str] | None = None) -> dict[str, Any]:
        provider = self.get_provider(provider_id)
        if provider is None:
            return {"status": "NOT_FOUND", "provider": provider_id}

        auth_type = str(provider.get("auth_type", "NONE")).upper()
        token_env = provider.get("token_env")
        if not token_env:
            token_env = {
                "huggingface": "HF_TOKEN",
                "openai_image": "OPENAI_API_KEY",
                "openai_voice": "OPENAI_API_KEY",
                "elevenlabs": "ELEVENLABS_API_KEY",
                "fal_ai": "FAL_KEY",
            }.get(provider_id)

        if auth_type in {"NONE", "PUBLIC", "ANONYMOUS"}:
            return {"provider": provider_id, "status": "NO_CREDENTIAL_REQUIRED", "credential_present": True}
        if not token_env:
            return {"provider": provider_id, "status": "MANUAL_CONNECTION_REQUIRED", "credential_present": False}

        env = os.environ if environ is None else environ
        present = bool(str(env.get(str(token_env), "")).strip())
        return {
            "provider": provider_id,
            "status": "CREDENTIALS_PRESENT" if present else "CREDENTIALS_MISSING",
            "credential_present": present,
            "credential_name": token_env,
        }

    def summary(self) -> dict[str, int]:
        result = {state: 0 for state in sorted(self.VALID_STATES)}
        for item in self.state["providers"].values():
            status = item.get("status", "UNKNOWN") if isinstance(item, dict) else "UNKNOWN"
            if status not in result:
                status = "UNKNOWN"
            result[status] += 1
        return result


if __name__ == "__main__":
    manager = ConnectionManager()
    print("===================================")
    print("       PROVIDER CONNECTION MANAGER")
    print("===================================")
    print("Catalog providers:", len(manager.list_providers()))
    print("Tracked connections:", len(manager.state["providers"]))
    print("Summary:", manager.summary())
    print("Credential checks report presence only; they do not prove live API access.")
