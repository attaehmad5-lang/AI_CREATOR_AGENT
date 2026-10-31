"""Offline consistency checks for the advanced provider catalog metadata."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..unified_provider_registry import ADVANCED_CATEGORIES


def validate_advanced_catalogs(
    central_path: str | Path,
    advanced_path: str | Path,
    categories_path: str | Path,
) -> dict[str, Any]:
    """Compare catalog membership/category metadata without making network calls."""
    errors: list[str] = []

    def load(path: str | Path, label: str) -> dict[str, Any]:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{label} unreadable: {type(exc).__name__}")
            return {}
        if not isinstance(value, dict):
            errors.append(f"{label} must be a JSON object")
            return {}
        return value

    central = load(central_path, "central catalog")
    advanced = load(advanced_path, "advanced catalog")
    categories = load(categories_path, "advanced categories")
    central_items = central.get("providers")
    advanced_items = advanced.get("providers")
    category_map = categories.get("categories")
    if not isinstance(central_items, list):
        errors.append("central catalog providers must be a list")
        central_items = []
    if not isinstance(advanced_items, list):
        errors.append("advanced catalog providers must be a list")
        advanced_items = []
    if not isinstance(category_map, dict):
        errors.append("advanced categories must be an object")
        category_map = {}

    def index(items: list[Any], label: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                errors.append(f"{label} entry {i} has no valid id")
                continue
            if item["id"] in result:
                errors.append(f"{label} contains duplicate id {item['id']}")
                continue
            result[item["id"]] = item
        return result

    central_index = index(central_items, "central catalog")
    advanced_index = index(advanced_items, "advanced catalog")
    expected = {key: value for key, value in central_index.items()
                if value.get("category") in ADVANCED_CATEGORIES}

    if set(category_map) != ADVANCED_CATEGORIES:
        errors.append("advanced category keys differ from the supported category set")
    for category, definition in category_map.items():
        if not isinstance(category, str) or not isinstance(definition, dict):
            errors.append("advanced category definitions must be objects keyed by category name")
            continue
        if not isinstance(definition.get("name"), str) or not definition["name"].strip():
            errors.append(f"advanced category display name is missing for {category}")
        capabilities = definition.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(item, str) or not item.strip() for item in capabilities)
            or len({item.casefold() for item in capabilities if isinstance(item, str)}) != len(capabilities)
        ):
            errors.append(f"advanced category capabilities are invalid for {category}")
    if set(expected) != set(advanced_index):
        errors.append("advanced catalog provider IDs differ from the central advanced provider IDs")
    for provider_id in set(expected) & set(advanced_index):
        if expected[provider_id].get("category") != advanced_index[provider_id].get("category"):
            errors.append(f"category mismatch for {provider_id}")

    policy = advanced.get("policy")
    if not isinstance(policy, dict):
        errors.append("advanced catalog policy must be an object")
    else:
        for key in ("auto_purchase", "subscription_bypass", "api_key_logging"):
            if policy.get(key) is not False:
                errors.append(f"policy {key} must be false")

    return {
        "valid": not errors,
        "errors": errors,
        "central_provider_count": len(central_index),
        "central_advanced_provider_count": len(expected),
        "advanced_provider_count": len(advanced_index),
        "category_count": len(category_map),
    }


def main() -> int:
    root = Path(__file__).resolve().parent
    result = validate_advanced_catalogs(
        root.parent / "provider_catalog.json",
        root / "advanced_provider_catalog.json",
        root / "advanced_categories.json",
    )
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
