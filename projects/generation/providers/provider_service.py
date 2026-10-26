"""Generation facade for the trusted provider registry."""
import json
from pathlib import Path

from .provider_manager import ProviderManager
from .fallback_manager import FallbackManager
from .provider_health import ProviderHealth

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "provider_registry.json"


class ProviderService:
    def __init__(self, registry_path=None):
        path = DEFAULT_REGISTRY_PATH if registry_path is None else Path(registry_path)
        with path.open("r", encoding="utf-8") as stream:
            registry = json.load(stream)
        if not isinstance(registry, dict):
            raise ValueError("provider registry root must be an object")
        image_config = registry.get("image")
        if not isinstance(image_config, dict):
            raise ValueError("provider registry image section must be an object")
        self.registry = registry
        if not hasattr(ProviderService, "_shared_health"):
            ProviderService._shared_health = ProviderHealth()
        self.manager = ProviderManager()
        self.health = ProviderService._shared_health
        self.manager.load_registry(image_config)
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
