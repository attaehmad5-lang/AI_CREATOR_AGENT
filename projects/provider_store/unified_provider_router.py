"""Compatibility discovery router that trusts only live verified connection state.

Actual request selection and reservation policy belong to ProviderRoutingManager.
This wrapper never treats static catalog labels such as READY/CONNECTED as proof.
"""
from __future__ import annotations

from typing import Any

from .connection_manager import ConnectionManager
from .unified_provider_registry import UnifiedProviderRegistry


class UnifiedProviderRouter:
    def __init__(
        self,
        connection_manager: ConnectionManager | None = None,
        *,
        catalog_path: str | None = None,
        state_path: str | None = None,
    ) -> None:
        self.registry = UnifiedProviderRegistry(
            connection_manager, catalog_path=catalog_path, state_path=state_path
        )
        self.connection_manager = self.registry.connection_manager

    def find(self, capability: str) -> list[dict[str, Any]]:
        if not isinstance(capability, str) or not capability.strip():
            return []
        matches = []
        for provider in self.registry.all_providers():
            capabilities = provider.get("capabilities", [])
            if not isinstance(capabilities, list):
                capabilities = []
            capabilities = [item for item in capabilities if isinstance(item, str)]
            if provider.get("category") == "image":
                capabilities = list(set(capabilities + ["text_to_image", "image_to_image", "upscaling"]))
            if capability in capabilities:
                matches.append({
                    "id": provider.get("id"),
                    "name": provider.get("name"),
                    "category": provider.get("category"),
                    "catalog_status": provider.get("catalog_status", provider.get("status")),
                    "enabled": provider.get("enabled") is True,
                    "subscription_required": provider.get("subscription_required") is True,
                })
        return matches

    def route(self, capability: str) -> dict[str, Any]:
        """Return verified candidates only; does not execute or reserve a generation."""
        matches = self.find(capability)
        if not matches:
            return {
                "status": "NO_PROVIDER",
                "capability": capability,
                "providers": [],
                "route_selected": False,
            }

        available = []
        locked = []
        unverified = []
        for provider in matches:
            provider_id = provider.get("id")
            if provider.get("enabled") is not True or provider.get("subscription_required") is True:
                locked.append(provider)
                continue
            try:
                state = self.connection_manager.get_state(provider_id)
            except Exception:
                state = {}
            if (
                isinstance(state, dict)
                and state.get("status") == "CONNECTED"
                and state.get("verified") is True
            ):
                available.append(provider)
            else:
                unverified.append(provider)

        return {
            "status": "ROUTABLE" if available else "PROVIDERS_FOUND",
            "capability": capability,
            "available": available,
            "locked": locked,
            "unverified": unverified,
            "total_matches": len(matches),
            "route_selected": False,
            "next_step": "Use ProviderRoutingManager for policy-aware selection and reservations.",
        }


if __name__ == "__main__":
    router = UnifiedProviderRouter()
    for capability in [
        "text_to_image", "story_generation", "image_upscaling",
        "speech_to_text", "voice_generation", "video_editing",
    ]:
        result = router.route(capability)
        print(capability, "=>", result["status"])
