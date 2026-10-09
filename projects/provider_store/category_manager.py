from provider_store_manager import ProviderStoreManager

class CategoryManager:

    def __init__(self):
        self.store = ProviderStoreManager()

    def list_categories(self):
        categories = {}
        for provider in self.store.list_providers():
            category = provider.get("category", "unknown")
            categories.setdefault(category, []).append(provider)
        return categories

    def providers_by_category(self, category):
        return [
            provider
            for provider in self.store.list_providers()
            if provider.get("category") == category
        ]

if __name__ == "__main__":
    manager = CategoryManager()
    categories = manager.list_categories()

    print("===================================")
    print("        PROVIDER CATEGORY MANAGER")
    print("===================================")

    for category, providers in sorted(categories.items()):
        print(f"{category.upper()}: {len(providers)} provider(s)")

    print("-----------------------------------")
    print("Categories:", len(categories))
    print("Providers:", len(manager.store.list_providers()))
    print("STATUS: READY")
