"""User-controlled provider flow facade backed by live connection state."""
from __future__ import annotations

from typing import Any

try:
    from .unlock_flow_manager import UnlockFlowManager
except ImportError:  # direct script execution compatibility
    from unlock_flow_manager import UnlockFlowManager


class ProviderFlow:
    def __init__(self, manager: UnlockFlowManager | None = None) -> None:
        self.manager = manager or UnlockFlowManager()

    def start(self, provider_id: str) -> dict[str, Any]:
        return self.manager.prepare(provider_id)

    def available_providers(self) -> list[dict[str, Any]]:
        providers = []
        for provider in self.manager.catalog.get("providers", []):
            provider_id = provider.get("id")
            state = self.manager.manager.get_state(provider_id) if isinstance(provider_id, str) else {}
            current_status = (
                "DISABLED" if provider.get("enabled") is False
                else state.get("status", "UNKNOWN")
            )
            providers.append({
                "id": provider_id,
                "name": provider.get("name"),
                "category": provider.get("category"),
                "catalog_status": provider.get("catalog_status", provider.get("status")),
                "status": current_status,
                "verified": state.get("verified") is True and current_status == "CONNECTED",
                "enabled": provider.get("enabled") is True,
            })
        return providers


if __name__ == "__main__":
    flow = ProviderFlow()
    print("===================================")
    print("       UNIVERSAL PROVIDER FLOW")
    print("===================================")
    print("Providers:", len(flow.available_providers()))
    print("Flow Type: UNIVERSAL")
    print("Subscription Bypass: DISABLED")
    print("Auto Purchase: DISABLED")
    print("STATUS: READY")
