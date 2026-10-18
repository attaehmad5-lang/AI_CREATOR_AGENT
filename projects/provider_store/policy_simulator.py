"""Dry-run validation for provider catalog policy changes.

This module never writes catalogs, changes provider state, loads adapters, or triggers
network/billing activity. A successful simulation is a review result, not activation.
"""
from __future__ import annotations

import copy
from typing import Any

from .connection_manager import ConnectionManager


class ProviderPolicySimulator:
    """Validate and explain a proposed provider-catalog change without applying it."""

    MAX_PROVIDERS = 500
    MAX_FINDINGS = 500

    @staticmethod
    def _provider_map(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
        providers = catalog.get("providers")
        if not isinstance(providers, list):
            return {}
        return {
            item["id"]: item
            for item in providers
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    @staticmethod
    def _is_api_provider(provider: dict[str, Any]) -> bool:
        return str(provider.get("connection_type", "")).upper() in {
            "API", "ACCOUNT_OR_API", "API_OR_ACCOUNT"
        }

    def simulate(
        self,
        current_catalog: dict[str, Any],
        proposed_catalog: dict[str, Any],
    ) -> dict[str, Any]:
        """Return bounded findings and a deterministic diff; never mutate either input."""
        if not isinstance(current_catalog, dict) or not isinstance(proposed_catalog, dict):
            return {
                "status": "INVALID_INPUT",
                "simulation_only": True,
                "activation_performed": False,
                "findings": [{"severity": "BLOCKER", "code": "CATALOG_MUST_BE_OBJECT"}],
                "changes": [],
            }

        current_copy = copy.deepcopy(current_catalog)
        proposed_copy = copy.deepcopy(proposed_catalog)
        old_raw = current_copy.get("providers")
        new_raw = proposed_copy.get("providers")
        if not isinstance(old_raw, list) or not isinstance(new_raw, list):
            return {
                "status": "POLICY_SIMULATION_BLOCKED",
                "simulation_only": True,
                "activation_performed": False,
                "findings": [{"severity": "BLOCKER", "code": "PROVIDERS_MUST_BE_LIST"}],
                "changes": [],
            }
        if len(old_raw) > self.MAX_PROVIDERS or len(new_raw) > self.MAX_PROVIDERS:
            return {
                "status": "POLICY_SIMULATION_BLOCKED",
                "simulation_only": True,
                "activation_performed": False,
                "findings": [{"severity": "BLOCKER", "code": "PROVIDER_LIMIT_EXCEEDED"}],
                "changes": [],
            }

        schema = ConnectionManager.validate_catalog_schema(proposed_copy)
        findings: list[dict[str, Any]] = []
        for error in schema.get("errors", [])[: self.MAX_FINDINGS]:
            findings.append({
                "severity": "BLOCKER",
                "code": "CATALOG_SCHEMA_INVALID",
                "detail": str(error)[:300],
            })

        old_map = self._provider_map(current_copy)
        new_map = self._provider_map(proposed_copy)
        changes: list[dict[str, Any]] = []
        for provider_id in sorted(set(old_map) | set(new_map)):
            old = old_map.get(provider_id)
            new = new_map.get(provider_id)
            if old is None:
                changes.append({"provider_id": provider_id, "change": "ADDED"})
                if new and new.get("enabled") is True:
                    findings.append({
                        "severity": "REVIEW",
                        "code": "NEW_PROVIDER_ENABLED",
                        "provider_id": provider_id,
                    })
            elif new is None:
                changes.append({"provider_id": provider_id, "change": "REMOVED"})
                findings.append({
                    "severity": "REVIEW",
                    "code": "PROVIDER_REMOVED",
                    "provider_id": provider_id,
                })
            else:
                changed_fields = sorted(
                    key for key in set(old) | set(new)
                    if old.get(key) != new.get(key)
                )
                if changed_fields:
                    changes.append({
                        "provider_id": provider_id,
                        "change": "UPDATED",
                        "fields": changed_fields[:50],
                    })
                if old.get("enabled") is not True and new.get("enabled") is True:
                    findings.append({
                        "severity": "REVIEW",
                        "code": "PROVIDER_ENABLEMENT_PROPOSED",
                        "provider_id": provider_id,
                    })
                if old.get("enabled") is True and new.get("enabled") is not True:
                    findings.append({
                        "severity": "REVIEW",
                        "code": "PROVIDER_DISABLEMENT_PROPOSED",
                        "provider_id": provider_id,
                    })
                if old.get("auth_type") != new.get("auth_type"):
                    findings.append({
                        "severity": "REVIEW",
                        "code": "AUTHENTICATION_POLICY_CHANGED",
                        "provider_id": provider_id,
                    })
                if old.get("allowed_hosts") != new.get("allowed_hosts"):
                    findings.append({
                        "severity": "REVIEW",
                        "code": "ENDPOINT_ALLOWLIST_CHANGED",
                        "provider_id": provider_id,
                    })
                old_caps = set(old.get("capabilities", [])) if isinstance(old.get("capabilities"), list) else set()
                new_caps = set(new.get("capabilities", [])) if isinstance(new.get("capabilities"), list) else set()
                removed_caps = sorted(old_caps - new_caps)
                if removed_caps:
                    findings.append({
                        "severity": "REVIEW",
                        "code": "CAPABILITIES_REMOVED",
                        "provider_id": provider_id,
                        "capabilities": removed_caps[:50],
                    })

            if new is not None and new.get("enabled") is True and self._is_api_provider(new):
                docs = new.get("documentation_url")
                hosts = new.get("allowed_hosts")
                capabilities = new.get("capabilities")
                if not isinstance(docs, str) or not docs.startswith("https://"):
                    findings.append({
                        "severity": "BLOCKER",
                        "code": "ENABLED_API_PROVIDER_REQUIRES_OFFICIAL_HTTPS_DOCS",
                        "provider_id": provider_id,
                    })
                if not isinstance(hosts, list) or not hosts:
                    findings.append({
                        "severity": "BLOCKER",
                        "code": "ENABLED_API_PROVIDER_REQUIRES_HOST_ALLOWLIST",
                        "provider_id": provider_id,
                    })
                if not isinstance(capabilities, list) or not capabilities:
                    findings.append({
                        "severity": "BLOCKER",
                        "code": "ENABLED_API_PROVIDER_REQUIRES_CAPABILITIES",
                        "provider_id": provider_id,
                    })
                if new.get("deprecated") is True:
                    findings.append({
                        "severity": "BLOCKER",
                        "code": "DEPRECATED_PROVIDER_CANNOT_BE_ENABLED",
                        "provider_id": provider_id,
                    })

        # Deduplicate deterministic findings and cap output for hostile/oversized diffs.
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        for finding in findings:
            key = (
                str(finding.get("code", "")),
                str(finding.get("provider_id", "")),
                str(finding.get("detail", "")),
            )
            unique[key] = finding
        safe_findings = list(unique.values())[: self.MAX_FINDINGS]
        has_blocker = any(item["severity"] == "BLOCKER" for item in safe_findings)
        has_review = any(item["severity"] == "REVIEW" for item in safe_findings)
        status = (
            "POLICY_SIMULATION_BLOCKED" if has_blocker
            else "POLICY_SIMULATION_REVIEW_REQUIRED" if has_review
            else "POLICY_SIMULATION_PASSED"
        )
        return {
            "status": status,
            "simulation_only": True,
            "activation_performed": False,
            "schema_valid": bool(schema.get("valid")),
            "changes": changes[: self.MAX_FINDINGS],
            "findings": safe_findings,
            "summary": {
                "providers_before": len(old_raw),
                "providers_after": len(new_raw),
                "changes": len(changes),
                "blockers": sum(item["severity"] == "BLOCKER" for item in safe_findings),
                "review_items": sum(item["severity"] == "REVIEW" for item in safe_findings),
            },
            "note": "Simulation only. No catalog writes, provider loads, network requests, or billing actions were performed.",
        }
