# Advanced Provider Store

This directory contains the legacy advanced-provider catalog, the category capability catalog, and the AdvancedProviderManager facade. Runtime provider state is owned by the canonical Provider Store under the parent directory.

## Main files

- advanced_provider_manager.py — advanced-category facade, validated category metadata, runtime diagnostics, catalog consistency checks, and verified-provider filtering.
- advanced_provider_catalog.json — 18 advanced provider records; identity/auth/connection/capability metadata must match the canonical catalog.
- advanced_provider_catalog_backup.json — backup copy; regression tests require it to remain synchronized with the active advanced catalog.
- advanced_categories.json — category descriptions and the allowed capability identifiers.
- ADVANCED_PROVIDER_STORE_EXECUTION_PLAN.md — ten bounded workstreams and acceptance workflow.
- ADVANCED_PROVIDER_STORE_COMPLETION_LEDGER.md — Markdown status ledger for all 150 points.
- ADVANCED_PROVIDER_STORE_COMPLETION_REMAINING_LEDGER.txt — portable plain-text continuation ledger with the same 150 points.

## Minimal usage

```python
from projects.provider_store.advanced.advanced_provider_manager import AdvancedProviderManager

manager = AdvancedProviderManager()

print(manager.status())
print(manager.catalog_consistency())
print(manager.list_categories())
print(manager.provider_diagnostics("runway_image_editing"))

# This returns only enabled, non-locked, non-deprecated advanced providers
# whose persisted runtime connection is explicitly verified.
# It does NOT bypass paid-use approval, project budgets, quotas, credits,
# output validation, or routing policy.
eligible = manager.list_verified_enabled_providers("image_editing")
```

## Safety rules

- A catalog label such as CONNECTED is not proof of a live connection; the runtime state must have verified=true.
- Disabled, locked, and deprecated providers are excluded from the verified-enabled listing.
- Credential values must be supplied through environment variables or an authorized connection flow. Never commit credentials or session objects.
- A generic HTTP 2xx is transport/authentication evidence only; it does not prove generation support, available credits, or billing behavior.
- Paid usage still requires explicit approval and the configured budget/quota policy.
- The advanced-provider catalog is metadata, not a set of ready-to-run production adapters. Provider-specific live tests require official endpoints and user-configured credentials.

## Tests

From the repository root:

```sh
python -m unittest discover -s tests -v
```

The required CI matrix currently runs the suite on Ubuntu and Windows. Use the completion ledger for verified results and remaining work; do not mark production readiness until the live-integration and packaging gates are closed.
