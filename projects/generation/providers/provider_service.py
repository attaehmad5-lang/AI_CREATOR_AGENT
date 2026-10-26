"""Generation facade that synchronizes runtime adapters with the central provider catalog."""
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

        self.registry = registry
        self._base_image_config = dict(image_config)
        self._configured_modules = dict(modules)
        self.connection_manager = (
            connection_manager if connection_manager is not None else ProviderConnectionManager()
        )
        if not hasattr(ProviderService, "_shared_health"):
            ProviderService._shared_health = ProviderHealth()
        self.manager = ProviderManager()
        self.health = ProviderService._shared_health
        self._runtime_signature = None
        self._runtime_image_config = None
        self._sync_runtime_registry()
        self.fallback = FallbackManager(self.manager, self.health)
        self.last_result = None

    def _sync_runtime_registry(self):
        """Reconcile loaded modules against the current central catalog atomically."""
        records = self.connection_manager.list_providers()
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ValueError("central provider catalog returned invalid records")
        by_name = {}
        for item in records:
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or name in by_name:
                raise ValueError("central provider catalog has invalid or duplicate names")
            by_name[name] = item

        enabled_modules = {}
        provider_records = {}
        signature_items = []
        for name, module_path in self._configured_modules.items():
            item = by_name.get(name)
            category = (
                item.get("category", "").casefold().replace("-", "_").replace(" ", "_")
                if item is not None and isinstance(item.get("category"), str)
                else ""
            )
            if item is None or category != "image":
                raise ValueError("runtime provider is missing or incompatible in central catalog")
            provider_id = item.get("id")
            if not isinstance(provider_id, str) or not provider_id.strip():
                raise ValueError("central provider catalog record has invalid ID")
            enabled = item.get("enabled") is True
            signature_items.append((name, module_path, provider_id, category, enabled))
            if enabled:
                enabled_modules[name] = module_path
                provider_records[name] = item

        primary = self._base_image_config.get("primary")
        fallbacks = self._base_image_config.get("fallbacks", [])
        runtime_config = dict(self._base_image_config)
        runtime_config["provider_modules"] = enabled_modules
        runtime_config["primary"] = primary if primary in enabled_modules else None
        runtime_config["fallbacks"] = [name for name in fallbacks if name in enabled_modules]
        signature = (
            tuple(sorted(signature_items)),
            runtime_config["primary"],
            tuple(runtime_config["fallbacks"]),
        )
        if signature == self._runtime_signature:
            return

        # ProviderManager stages all instances before replacing its active registry.
        self.manager.load_registry(runtime_config)
        for name, item in provider_records.items():
            contract = self.connection_manager.validate_provider_contract(
                item["id"], self.manager.get(name)
            )
            if not isinstance(contract, dict) or contract.get("valid") is not True:
                raise ValueError("runtime provider contract validation failed")
        self._runtime_image_config = runtime_config
        self._runtime_signature = signature

    def generate_image(self, prompt, output_path):
        try:
            self._sync_runtime_registry()
        except Exception as exc:
            return {
                "status": "PROVIDER_CATALOG_SYNC_FAILED",
                "success": False,
                "error_type": type(exc).__name__,
            }
        config = self._runtime_image_config or {}
        providers = []
        primary = config.get("primary")
        fallbacks = config.get("fallbacks", [])
        if primary:
            providers.append(primary)
        providers.extend(fallbacks)
        if not providers:
            return {"status": "NO_ENABLED_PROVIDER", "success": False}
        self.last_result = self.fallback.generate_image(providers, prompt, output_path)
        return self.last_result
