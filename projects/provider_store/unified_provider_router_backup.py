from unified_provider_registry import UnifiedProviderRegistry

class UnifiedProviderRouter:

    def __init__(self):
        self.registry = UnifiedProviderRegistry()

    def find(self, capability):
        matches = []

        for provider in self.registry.all_providers():
            capabilities = provider.get("capabilities", [])

            if provider.get("category") == "image":
                capabilities = list(set(capabilities + ["text_to_image", "image_to_image", "upscaling"]))

            if capability in capabilities:
                matches.append({
                    "id": provider.get("id"),
                    "name": provider.get("name"),
                    "category": provider.get("category"),
                    "status": provider.get("status")
                })

        return matches

    def route(self, capability):
        matches = self.find(capability)

        if not matches:
            return {
                "status": "NO_PROVIDER",
                "capability": capability,
                "providers": []
            }

        available = [
            p for p in matches
            if p.get("status") in ("CONNECTED", "READY")
        ]

        locked = [
            p for p in matches
            if p.get("status") == "LOCKED"
        ]

        return {
            "status": "ROUTABLE" if available else "PROVIDERS_FOUND",
            "capability": capability,
            "available": available,
            "locked": locked,
            "total_matches": len(matches)
        }


if __name__ == "__main__":
    router = UnifiedProviderRouter()

    print("===================================")
    print("      UNIFIED PROVIDER ROUTER")
    print("===================================")

    for capability in [
        "text_to_image",
        "story_generation",
        "image_upscaling",
        "speech_to_text",
        "voice_generation",
        "video_editing"
    ]:
        result = router.route(capability)
        print(capability, "=>", result["status"])

    print("STATUS: READY")
