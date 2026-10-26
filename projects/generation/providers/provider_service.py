"""Generation facade for the trusted provider registry."""
import json
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager as ProviderConnectionManager
from .provider_manager import ProviderManager
from .fallback_manager import FallbackManager
from .provider_health import ProviderHealth

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "provider_registry.json"


class ProviderService:
    def __init__(self, registry_path=None, *, connection_manager=None):
        path = DEFAULT_REGISTRY_PATH if registry_path is None else Path(registry_path)
        with path.open("r", encoding="utf-8") as stream:
            registry = json.load(stream)
        if not isinstance(registry, dict):
            raise ValueError("provider registry root must be an object")
        image_config = registry.get("image")
        if not isinstance(image_config, dict):
            raise ValueError("provider registry image section must be an object")
        modules = image_config.get("provider_modules", {})
        if not isinstance(modules, dict):
            raise ValueError("provider registry provider_modules must be an object")
        primary = image_config.get("primary")
        fallbacks = image_config.get("fallbacks", [])
        if primary is not None and (not isinstance(primary, str) or not primary.strip()):
            raise ValueError("provider registry primary must be a provider name or null")
        if (
            not isinstance(fallbacks, list)
            or any(not isinstance(name, str) or not name.strip() for name in fallbacks)
            or len(set(fallbacks)) != len(fallbacks)
        ):
            raise ValueError("provider registry fallbacks must be unique provider names")
        route_names = ([primary] if primary is not None else []) + fallbacks
        if any(name not in modules for name in route_names):
            raise ValueError("every routed provider must have a module entry")

        catalog = connection_manager if connection_manager is not None else ProviderConnectionManager()
        records = catalog.list_providers()
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ValueError("central provider catalog returned invalid records")
        by_name = {}
        for item in records:
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or name in by_name:
                raise ValueError("central provider catalog has invalid or duplicate names")
            by_name[name] = item

        for name in modules:
            item = by_name.get(name)
            category = (
                item.get("category", "").casefold().replace("-", "_").replace(" ", "_")
                if item is not None and isinstance(item.get("category"), str)
                else ""
            )
            if item is None or item.get("enabled") is not True or category != "image":
                raise ValueError("runtime provider is missing, disabled, or incompatible in central catalog")
            if not isinstance(item.get("id"), str) or not item["id"].strip():
                raise ValueError("central provider catalog record has invalid ID")

        self.registry = registry
        self.connection_manager = catalog
        if not hasattr(ProviderService, "_shared_health"):
            ProviderService._shared_health = ProviderHealth()
        self.manager = ProviderManager()
        self.health = ProviderService._shared_health
        self.manager.load_registry(image_config)
        for name in modules:
            record = by_name[name]
            instance = self.manager.get(name)
            contract = catalog.validate_provider_contract(record["id"], instance)
            if not isinstance(contract, dict) or contract.get("valid") is not True:
                raise ValueError("runtime provider contract validation failed")
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
