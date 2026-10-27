""""Bridge advanced provider selection to the central ConnectionManager.

This adapter deliberately owns no catalog, credentials, connection state, or
billing ledger. ConnectionManager remains the source of truth; ProviderService
remains responsible for live execution and usage finalization.
"""
from __future__ import annotations

import math
from typing import Any

try:
    from ..connection_manager import ConnectionManager
except ImportError:  # pragma: no cover - direct script compatibility
    from connection_manager import ConnectionManager


class AdvancedStoreBridge:
    """Read-only advanced catalog/routing view over the canonical provider store."""

    def __init__(self, connection_manager: ConnectionManager | None = None) -> None:
        self.manager = connection_manager or ConnectionManager()

    @staticmethod
    def _capabilities(provider: dict[str, Any]) -> set[str]:
        raw = provider.get("capabilities", [])
        if not isinstance(raw, list):
            return set()
        return {value for value in raw if isinstance(value, str) and value.strip()}

    @staticmethod
    def _safe_price(provider: dict[str, Any]) -> float | None:
        value = provider.get("price_per_unit")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None

    def list_providers(
        self,
        *,
        capability: str | None = None,
        region: str | None = None,
        enabled_only: bool = False,
        verified_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return detached provider snapshots; never expose credential values."""
        results: list[dict[str, Any]] = []
        for original in self.manager.list_providers():
            provider_id = original.get("id")
            if not isinstance(provider_id, str) or not provider_id:
                continue
            if enabled_only and original.get("enabled") is not True:
                continue
            if capability and capability not in self._capabilities(original):
                continue
            regions = original.get("regions")
            if region and (not isinstance(regions, list) or region not in regions):
                continue
            state = self.manager.get_state(provider_id)
            verified = state.get("verified") is True and state.get("status") in {"CONNECTED", "READY"}
            if verified_only and not verified:
                continue
            # Publish an allow-listed metadata view only. Arbitrary provider
            # fields are never copied, avoiding accidental credential exposure.
            results.append({
                "id": provider_id,
                "name": original.get("name", provider_id),
                "category": original.get("category", "unknown"),
                "enabled": original.get("enabled") is True,
                "capabilities": sorted(self._capabilities(original)),
                "priority": original.get("priority", 100),
                "regions": list(regions) if isinstance(regions, list) else [],
                "price_per_unit": self._safe_price(original),
                "status": state.get("status", "UNKNOWN"),
                "verified": verified,
                "auth_type": original.get("auth_type"),
                "connection_type": original.get("connection_type"),
            })
        return results

    def candidates(
        self,
        capability: str,
        *,
        strategy: str = "priority",
        region: str | None = None,
        max_price: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return verified, enabled candidates; cost routing fails closed on unknown prices."""
        if strategy not in {"priority", "cost"}:
            raise ValueError("strategy must be 'priority' or 'cost'")
        if max_price is not None and (
            isinstance(max_price, bool) or not isinstance(max_price, (int, float))
            or not math.isfinite(max_price) or max_price < 0
        ):
            raise ValueError("max_price must be a finite non-negative number")
        providers = self.list_providers(
            capability=capability, region=region, enabled_only=True, verified_only=True
        )
        eligible: list[dict[str, Any]] = []
        for provider in providers:
            price = provider["price_per_unit"]
            if max_price is not None and (price is None or price > max_price):
                continue
            eligible.append(provider)
        if strategy == "cost":
            eligible.sort(key=lambda p: (
                p["price_per_unit"] is None,
                p["price_per_unit"] if p["price_per_unit"] is not None else float("inf"),
                p["priority"] if isinstance(p["priority"], (int, float)) else 100,
                p["id"],
            ))
        else:
            eligible.sort(key=lambda p: (
                p["priority"] if isinstance(p["priority"], (int, float)) else 100,
                p["id"],
            ))
        return eligible

    def simulate(self, capability: str, **kwargs: Any) -> list[str]:
        """Dry-run selection: no provider calls, credentials, billing, or state changes."""
        return [provider["id"] for provider in self.candidates(capability, **kwargs)]

    def execute_via_service(self, provider_service: Any, **request: Any) -> dict[str, Any]:
        """Delegate live work to the canonical ProviderService safety/accounting path.

        This method does not execute a provider itself. ProviderService owns
        authorization, canonical routing, reservations, output validation,
        billing/quota reconciliation, and ambiguous-outcome handling. Never
        substitute the dry-run candidates above for this production boundary.
        """
        executor = getattr(provider_service, "execute_routed_request", None)
        if not callable(executor):
            return {"status": "PROVIDER_SERVICE_UNAVAILABLE", "executed": False}
        try:
            result = executor(**request)
        except Exception:
            # Once delegated, an exception cannot prove that no paid request
            # reached a provider. Fail closed and retain any reservations.
            response: dict[str, Any] = {
                "status": "EXECUTION_OUTCOME_UNKNOWN",
                "executed": False,
                "reservations_retained": True,
            }
            request_id = request.get("request_id")
            if isinstance(request_id, str) and request_id.strip():
                response["request_id"] = request_id
            return response
        if not isinstance(result, dict):
            response = {
                "status": "EXECUTION_OUTCOME_UNKNOWN",
                "executed": False,
                "reservations_retained": True,
            }
            request_id = request.get("request_id")
            if isinstance(request_id, str) and request_id.strip():
                response["request_id"] = request_id
            return response
        return result
