import json
from .provider_manager import ProviderManager
from .fallback_manager import FallbackManager
from .provider_health import ProviderHealth

class ProviderService:
    def __init__(self, registry_path="projects/generation/provider_registry.json"):
        with open(registry_path, "r") as f:
            self.registry = json.load(f)
        self.manager = ProviderManager()
        self.health = ProviderHealth()
        self.manager.load_registry(self.registry.get("image", {}))
        self.fallback = FallbackManager(self.manager, self.health)
        self.last_result = None

    def generate_image(self, prompt, output_path):
        config = self.registry.get("image", {})
        providers = []
        primary = config.get("primary")
        fallbacks = config.get("fallbacks", [])
        if primary:
            providers.append(primary)
        providers.extend(fallbacks)
        self.last_result = self.fallback.generate_image(providers, prompt, output_path)
        return self.last_result
