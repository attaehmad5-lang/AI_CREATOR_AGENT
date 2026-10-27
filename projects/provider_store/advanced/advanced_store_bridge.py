"""Bridge advanced provider selection to the central ConnectionManager.

This adapter deliberately owns no catalog, credentials, connection state, or
billing ledger. ConnectionManager remains the source of truth; ProviderService
remains responsible for live execution and usage finalization.
"""
from __future__ import annotations

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
            if region and isinstance(regions, list) and region not in regions:
                continue
            state = self.manager.get_state(provider_id)
            verified = state.get("verified") is True and state.get("status") in {"CONNECTED", "READY"}
            if verified_only and not verified:
                continue
            # Only publish allow-listed metadata. Never copy arbitrary provider
            # fields (which could accidentally include credentials or headers).
            results.append({
                "id": provider_id,
                "name": original.get("name", provider_id),
                "category": original.get("category", "unknown"),
                "enabled": original.get("enabled") is True,
                "capabilities": sorted(self._capabilities(original)),
                "priority": original.get("priority", 100),
                "regions": list(regions) if isinstance(regions, list) else [],
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
        providers = self.list_providers(
            capability=capability, region=region, enabled_only=True, verified_only=True
        )
        eligible: list[dict[str, Any]] = []
        for provider in providers:
            # Price metadata is intentionally optional in the central catalog.
            # Unknown prices are excluded when a budget ceiling is requested.
            price = provider.get("price_per_unit")
            if max_price is not None:
                if not isinstance(price, (int, float)) or isinstance(price, bool) or price < 0 or price > max_price:
                    continue
            provider["price_per_unit"] = price
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
