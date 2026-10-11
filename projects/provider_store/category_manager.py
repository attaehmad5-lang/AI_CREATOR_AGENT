"""Category queries backed by the central validated provider store."""
from __future__ import annotations

from typing import Any

from .provider_store_manager import ProviderStoreManager


class CategoryManager:
    def __init__(self, store: ProviderStoreManager | None = None) -> None:
        self.store = store or ProviderStoreManager()

    def list_categories(self) -> dict[str, list[dict[str, Any]]]:
        categories: dict[str, list[dict[str, Any]]] = {}
        for provider in self.store.list_providers():
            category = provider.get("category", "unknown")
            categories.setdefault(category, []).append(provider)
        return categories

    def providers_by_category(self, category: str) -> list[dict[str, Any]]:
        return [
            provider for provider in self.store.list_providers()
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
