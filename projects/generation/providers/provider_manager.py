import importlib

class ProviderManager:
    def __init__(self):
        self.providers = {}

    def register(self, name, module_name, class_name):
        module = importlib.import_module(module_name)
        provider_class = getattr(module, class_name)
        self.providers[name] = provider_class()

    def load_registry(self, registry):
        modules = registry.get("provider_modules", {})
        for name, module_path in modules.items():
            module_name = module_path[:-3].replace("/", ".")
            class_name = name.replace(" ", "") + "Provider"
            self.register(name, module_name, class_name)

    def get(self, name):
        return self.providers.get(name)
