import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.routing_manager import ProviderRoutingManager
from projects.provider_store.quota_manager import ProviderQuotaManager
from projects.provider_store.credit_manager import ProviderCreditManager
from projects.provider_store.project_policy_manager import ProviderProjectPolicyManager
from projects.provider_store.tenant_policy_manager import ProviderTenantPolicyManager


class ProviderRoutingManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog_path = self.root / "providers.json"
        self.state_path = self.root / "connections.json"
        self.health_path = self.root / "health.json"
        self.catalog_path.write_text(json.dumps({
            "providers": [
                {"id": "primary", "name": "Primary", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "zero_cost_allowed": True, "priority": 1, "capabilities": ["image"]},
                {"id": "fallback", "name": "Fallback", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "zero_cost_allowed": True, "priority": 2, "capabilities": ["image"]},
                {"id": "disabled", "name": "Disabled", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": False, "priority": 0, "capabilities": ["image"]},
                {"id": "voice", "name": "Voice", "category": "voice", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "zero_cost_allowed": True, "priority": 1, "capabilities": ["voice"]},
            ]
        }), encoding="utf-8")
        self.state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(self.catalog_path, self.state_path)
        for provider_id in ("primary", "fallback", "voice"):
            verified = self.connections.verify_connectivity(
                provider_id, lambda provider, **kwargs: {"ok": True}
            )
            if verified.get("verified") is not True:
                raise AssertionError(f"test fixture provider {provider_id} failed stub verification")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _confidence_router(self, benchmark_results, quota_manager=None):
        catalog = self.root / "confidence-providers.json"
        state = self.root / "confidence-connections.json"
        models = [{"id": "stable-diffusion-xl", "versions": ["1"]}]
        catalog.write_text(json.dumps({
            "providers": [
                {"id": "primary", "name": "Primary", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "zero_cost_allowed": True,
                 "priority": 1, "capabilities": ["image"], "supported_models": models},
                {"id": "fallback", "name": "Fallback", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "zero_cost_allowed": True,
                 "priority": 2, "capabilities": ["image"], "supported_models": models},
            ]
        }), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        connections = ConnectionManager(catalog, state)
        for provider_id in ("primary", "fallback"):
            self.assertTrue(connections.verify_connectivity(
                provider_id, lambda provider, **kwargs: {"ok": True}
            )["verified"])

        class BenchmarkStub:
            def compare(self, *, modality=None):
                return {"status": "BENCHMARKS_AVAILABLE", "results": benchmark_results}

        return ProviderRoutingManager(connections, benchmark_manager=BenchmarkStub(), quota_manager=quota_manager)

    def test_confidence_aware_routing_adapts_to_verified_benchmark_history(self):
        router = self._confidence_router([
            {"provider_id": "fallback", "model_id": "stable-diffusion-xl", "modality": "image",
             "sample_count": 50, "success_rate": 0.96, "p95_latency_ms": 180,
             "avg_cost_per_success": 0.0, "avg_quality_score": 0.91},
            {"provider_id": "primary", "model_id": "stable-diffusion-xl", "modality": "image",
             "sample_count": 50, "success_rate": 0.55, "p95_latency_ms": 100,
             "avg_cost_per_success": 0.0, "avg_quality_score": 0.6},
        ])
        result = router.route(
            category="image", project_id="film", model="stable-diffusion-xl",
            confidence_aware=True, min_benchmark_samples=20,
            min_benchmark_success_rate=0.8,
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["confidence_routing_status"], "CONFIDENCE_AWARE_ROUTING")
        self.assertEqual(result["benchmark_evidence"]["sample_count"], 50)

    def test_benchmark_evidence_matches_provider_selected_after_quota_rejection(self):
        results = [
            {"provider_id": "primary", "model_id": "stable-diffusion-xl", "modality": "image",
             "sample_count": 100, "success_rate": 0.99, "p95_latency_ms": 100,
             "avg_cost_per_success": 0.0, "avg_quality_score": 0.95},
            {"provider_id": "fallback", "model_id": "stable-diffusion-xl", "modality": "image",
             "sample_count": 20, "success_rate": 0.9, "p95_latency_ms": 200,
             "avg_cost_per_success": 0.0, "avg_quality_score": 0.85},
        ]

        class QuotaStub:
            def reserve(self, *, provider_id, request_id):
                if provider_id == "primary":
                    return {"status": "QUOTA_EXCEEDED"}
                return {"status": "RESERVED"}

        router = self._confidence_router(results, quota_manager=QuotaStub())
        result = router.route(
            category="image", project_id="film", model="stable-diffusion-xl",
            request_id="quota-job", confidence_aware=True,
            min_benchmark_samples=10, min_benchmark_success_rate=0.8,
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["benchmark_evidence"]["sample_count"], 20)
        self.assertEqual(result["benchmark_evidence"]["success_rate"], 0.9)

    def test_low_confidence_can_fallback_or_fail_closed(self):
        results = [
            {"provider_id": provider, "model_id": "stable-diffusion-xl", "modality": "image",
             "sample_count": 2, "success_rate": 0.9, "p95_latency_ms": 100,
             "avg_cost_per_success": 0.0, "avg_quality_score": 0.8}
            for provider in ("primary", "fallback")
        ]
        router = self._confidence_router(results)
        blocked = router.route(
            category="image", project_id="film", model="stable-diffusion-xl",
            confidence_aware=True, min_benchmark_samples=10,
            allow_low_confidence_fallback=False,
        )
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["status"], "CONFIDENCE_INSUFFICIENT")
        fallback = router.route(
            category="image", project_id="film", model="stable-diffusion-xl",
            confidence_aware=True, min_benchmark_samples=10,
            allow_low_confidence_fallback=True,
        )
        self.assertTrue(fallback["allowed"])
        self.assertEqual(fallback["provider"], "primary")
        self.assertEqual(fallback["confidence_routing_status"], "CONFIDENCE_FALLBACK")

    def test_required_data_residency_region_filters_provider_candidates(self):
        catalog = self.root / "region-providers.json"
        state = self.root / "region-connections.json"
        catalog.write_text(json.dumps({
            "providers": [
                {"id": "primary", "name": "Primary", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "zero_cost_allowed": True,
                 "priority": 1, "capabilities": ["image"], "supported_regions": ["eu-west"]},
                {"id": "fallback", "name": "Fallback", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "zero_cost_allowed": True,
                 "priority": 2, "capabilities": ["image"], "data_residency_regions": ["saudi-arabia"]},
            ]
        }), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        connections = ConnectionManager(catalog, state)
        for provider_id in ("primary", "fallback"):
            self.assertTrue(connections.verify_connectivity(
                provider_id, lambda provider, **kwargs: {"ok": True}
            )["verified"])
        router = ProviderRoutingManager(connections)
        result = router.route(
            category="image", project_id="film", required_region="SAUDI-ARABIA"
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["required_region"], "SAUDI-ARABIA")
        self.assertIn(
            {"provider": "primary", "status": "DATA_RESIDENCY_REGION_NOT_SUPPORTED"},
            result["prior_decisions"],
        )

        no_region_catalog = self.root / "no-region-provider.json"
        no_region_state = self.root / "no-region-connections.json"
        no_region_catalog.write_text(json.dumps({
            "providers": [{
                "id": "unverified-region", "name": "Unverified", "category": "image",
                "auth_type": "NONE", "connection_type": "API", "enabled": True,
                "zero_cost_allowed": True, "priority": 1, "capabilities": ["image"],
            }]
        }), encoding="utf-8")
        no_region_state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        no_region_connections = ConnectionManager(no_region_catalog, no_region_state)
        no_region_connections.verify_connectivity(
            "unverified-region", lambda provider, **kwargs: {"ok": True}
        )
        rejected = ProviderRoutingManager(no_region_connections).route(
            category="image", project_id="film", required_region="saudi-arabia"
        )
        self.assertFalse(rejected["allowed"])
        self.assertEqual(rejected["decisions"][0]["status"], "REGION_METADATA_UNVERIFIED")

    def test_routing_rejects_unverified_provider_even_if_catalog_label_says_connected(self):
        self.connections.set_state("primary", "READY")
        router = ProviderRoutingManager(self.connections)
        result = router.route(
            category="image", project_id="film", excluded_providers={"fallback"}
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertEqual(result["decisions"][0]["status"], "CONNECTION_NOT_VERIFIED")

    def test_explicit_catalog_only_planning_can_skip_connection_evidence(self):
        self.connections.set_state("primary", "READY")
        router = ProviderRoutingManager(self.connections, require_verified_connection=False)
        result = router.route(
            category="image", project_id="film", excluded_providers={"fallback"}
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "primary")

    def test_primary_route_skips_disabled_and_orders_by_priority(self):
        router = ProviderRoutingManager(self.connections)
        result = router.route(category="image", project_id="film")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "primary")
        self.assertFalse(result["health_verified"])

    def test_primary_only_never_uses_fallback_when_primary_is_unavailable(self):
        router = ProviderRoutingManager(self.connections)
        self.connections.set_state("primary", "READY")
        result = router.route(
            category="image", project_id="film", strategy="PRIMARY_ONLY"
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertEqual(
            [decision["provider"] for decision in result["decisions"]],
            ["primary"],
        )
        self.assertFalse(result["automatic_fallback"])

    def test_automatic_fallback_can_be_disabled_without_changing_primary_strategy(self):
        router = ProviderRoutingManager(self.connections)
        self.connections.set_state("primary", "READY")
        result = router.route(
            category="image", project_id="film", automatic_fallback=False
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertEqual(
            [decision["provider"] for decision in result["decisions"]],
            ["primary"],
        )
        self.assertFalse(result["automatic_fallback"])

    def test_automatic_fallback_policy_requires_boolean(self):
        result = ProviderRoutingManager(self.connections).route(
            category="image", project_id="film", automatic_fallback="false"
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "INVALID_FALLBACK_POLICY")

    def test_fallback_excludes_failed_provider(self):
        router = ProviderRoutingManager(self.connections)
        result = router.select_fallback(
            failed_provider="primary", category="image", project_id="film"
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["failed_provider"], "primary")

    def test_model_and_version_aware_routing_uses_declared_compatibility(self):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        for provider in catalog["providers"]:
            if provider["id"] == "primary":
                provider["supported_models"] = [{"id": "flux", "versions": ["1.0", "1.1"]}]
            elif provider["id"] == "fallback":
                provider["supported_models"] = [{"id": "sdxl", "versions": ["2.0"]}]
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        router = ProviderRoutingManager(self.connections)
        routed = router.route(
            category="image", project_id="film", model="sdxl", model_version="2.0"
        )
        self.assertTrue(routed["allowed"])
        self.assertEqual(routed["provider"], "fallback")
        self.assertEqual(routed["model"], "sdxl")
        self.assertEqual(routed["model_version"], "2.0")

        unsupported_version = router.route(
            category="image", project_id="film", model="sdxl", model_version="3.0"
        )
        self.assertFalse(unsupported_version["allowed"])
        self.assertIn(
            "MODEL_VERSION_NOT_SUPPORTED",
            [decision["status"] for decision in unsupported_version["decisions"]],
        )

    def test_model_routing_fails_closed_without_declared_model_metadata(self):
        result = ProviderRoutingManager(self.connections).route(
            category="image", project_id="film", model="undeclared-model"
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertEqual(
            [decision["status"] for decision in result["decisions"]],
            ["MODEL_METADATA_UNVERIFIED", "MODEL_METADATA_UNVERIFIED"],
        )

    def test_model_version_requires_a_model_id(self):
        result = ProviderRoutingManager(self.connections).route(
            category="image", project_id="film", model_version="1.0"
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "INVALID_MODEL_VERSION")

    def test_project_policy_restricts_provider_and_disables_fallback(self):
        policies = ProviderProjectPolicyManager(self.root / "project-policies.json")
        saved = policies.set_policy("film", {
            "allowed_providers": ["fallback"],
            "automatic_fallback": False,
        })
        self.assertTrue(saved["saved"])
        router = ProviderRoutingManager(self.connections, project_policy_manager=policies)
        result = router.route(category="image", project_id="film")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertFalse(result["automatic_fallback"])

    def test_project_policy_requires_configuration_and_enforces_cost_limit(self):
        policies = ProviderProjectPolicyManager(self.root / "project-policies.json")
        router = ProviderRoutingManager(self.connections, project_policy_manager=policies)
        missing = router.route(category="image", project_id="unconfigured-project")
        self.assertFalse(missing["allowed"])
        self.assertEqual(missing["status"], "PROJECT_POLICY_REQUIRED")
        policies.set_policy("film", {
            "allowed_providers": ["primary"],
            "max_estimated_cost": 1.0,
        })
        too_expensive = router.route(
            category="image", project_id="film", estimated_cost=2.0,
        )
        self.assertFalse(too_expensive["allowed"])
        self.assertEqual(too_expensive["status"], "PROJECT_COST_LIMIT_EXCEEDED")

    def test_project_policy_restricts_models_and_capabilities(self):
        policies = ProviderProjectPolicyManager(self.root / "project-policies.json")
        policies.set_policy("film", {"allowed_models": ["sdxl"]})
        router = ProviderRoutingManager(self.connections, project_policy_manager=policies)
        model_denied = router.route(
            category="image", project_id="film", model="flux"
        )
        self.assertFalse(model_denied["allowed"])
        self.assertEqual(model_denied["status"], "PROJECT_MODEL_NOT_ALLOWED")

        policies.set_policy("film", {"allowed_capabilities": ["voice_generation"]})
        capability_denied = router.route(category="image", project_id="film")
        self.assertFalse(capability_denied["allowed"])
        self.assertEqual(capability_denied["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertTrue(all(
            decision["status"] == "PROJECT_CAPABILITY_NOT_ALLOWED"
            for decision in capability_denied["decisions"]
        ))

    def test_persisted_project_region_policy_is_enforced_by_router(self):
        policies = ProviderProjectPolicyManager(self.root / "project-policies.json")
        saved = policies.set_policy("film", {"required_region": "SAUDI-ARABIA"})
        self.assertTrue(saved["saved"])
        router = ProviderRoutingManager(self.connections, project_policy_manager=policies)
        result = router.route(category="image", project_id="film")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertTrue(all(
            decision["status"] == "REGION_METADATA_UNVERIFIED"
            for decision in result["decisions"]
        ))

    def test_tenant_policy_is_required_and_intersects_project_allowlist(self):
        tenant_policies = ProviderTenantPolicyManager(self.root / "tenant-policies.json")
        router_missing = ProviderRoutingManager(
            self.connections, tenant_policy_manager=tenant_policies
        )
        missing = router_missing.route(category="image", project_id="film")
        self.assertFalse(missing["allowed"])
        self.assertEqual(missing["status"], "TENANT_POLICY_REQUIRED")

        project_policies = ProviderProjectPolicyManager(self.root / "project-policies.json")
        project_policies.set_policy("film", {"allowed_providers": ["primary", "fallback"]})
        tenant_policies.set_policy("tenant-a", {"allowed_providers": ["fallback"]})
        router = ProviderRoutingManager(
            self.connections, project_policy_manager=project_policies,
            tenant_policy_manager=tenant_policies,
        )
        result = router.route(category="image", project_id="film", tenant_id="tenant-a")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")

    def test_tenant_policy_applies_stricter_cost_and_rejects_region_conflicts(self):
        project_policies = ProviderProjectPolicyManager(self.root / "project-policies.json")
        tenant_policies = ProviderTenantPolicyManager(self.root / "tenant-policies.json")
        project_policies.set_policy("film", {
            "allowed_providers": ["primary", "fallback"],
            "max_estimated_cost": 5.0,
            "required_region": "EU-WEST-1",
        })
        tenant_policies.set_policy("tenant-a", {
            "allowed_providers": ["primary", "fallback"],
            "max_estimated_cost": 1.0,
            "required_region": "SAUDI-ARABIA",
        })
        router = ProviderRoutingManager(
            self.connections, project_policy_manager=project_policies,
            tenant_policy_manager=tenant_policies,
        )
        conflict = router.route(
            category="image", project_id="film", tenant_id="tenant-a",
            estimated_cost=0.0,
        )
        self.assertFalse(conflict["allowed"])
        self.assertEqual(conflict["status"], "TENANT_PROJECT_REGION_POLICY_CONFLICT")

        project_policies.set_policy("film", {
            "allowed_providers": ["primary", "fallback"],
            "max_estimated_cost": 5.0,
        })
        tenant_policies.set_policy("tenant-a", {
            "allowed_providers": ["primary", "fallback"],
            "max_estimated_cost": 1.0,
        })
        too_expensive = router.route(
            category="image", project_id="film", tenant_id="tenant-a",
            estimated_cost=2.0,
        )
        self.assertFalse(too_expensive["allowed"])
        self.assertEqual(too_expensive["status"], "PROJECT_COST_LIMIT_EXCEEDED")

    def test_tenant_policy_alone_restricts_providers(self):
        policies = ProviderTenantPolicyManager(self.root / "tenant-policies.json")
        policies.set_policy("tenant-a", {"allowed_providers": ["fallback"]})
        router = ProviderRoutingManager(self.connections, tenant_policy_manager=policies)
        result = router.route(
            category="image", project_id="film", tenant_id="tenant-a"
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")

    def test_project_policy_can_require_health_evidence(self):
        policies = ProviderProjectPolicyManager(self.root / "project-policies.json")
        policies.set_policy("film", {"require_health_evidence": True})
        router = ProviderRoutingManager(self.connections, project_policy_manager=policies)
        result = router.route(category="image", project_id="film")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "HEALTH_DATA_REQUIRED")

    def test_free_credit_and_lowest_cost_strategies(self):
        router = ProviderRoutingManager(self.connections)
        free_first = router.route(
            category="image", project_id="film", strategy="FREE_CREDITS_FIRST",
            free_credits={"primary": 0.0, "fallback": 2.0},
        )
        self.assertEqual(free_first["provider"], "fallback")
        cost_policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        cost_router = ProviderRoutingManager(self.connections, cost_policy=cost_policy)
        cheapest = cost_router.route(
            category="image", project_id="film", strategy="LOWEST_COST",
            estimated_costs={"primary": 1.5, "fallback": 0.2},
            paid_approved_providers={"primary", "fallback"},
            request_id="lowest-cost-job",
        )
        self.assertTrue(cheapest["allowed"])
        self.assertEqual(cheapest["provider"], "fallback")
        self.assertEqual(cheapest["billing_mode"], "PAID_APPROVED")

    def test_round_robin_distributes_across_eligible_providers(self):
        router = ProviderRoutingManager(self.connections, default_strategy="ROUND_ROBIN")
        first = router.route(category="image", project_id="film")
        second = router.route(category="image", project_id="film")
        self.assertEqual([first["provider"], second["provider"]], ["primary", "fallback"])

    def test_health_evidence_can_be_required_and_unhealthy_providers_are_skipped(self):
        health = ProviderHealthManager(self.health_path)
        router = ProviderRoutingManager(self.connections, health_manager=health)
        unknown = router.route(
            category="image", project_id="film", require_health_evidence=True
        )
        self.assertFalse(unknown["allowed"])
        health.record_result("primary", success=False, error_code="HTTP_503")
        health.record_result("primary", success=False, error_code="HTTP_503")
        health.record_result("primary", success=False, error_code="HTTP_503")
        health.record_result("fallback", success=True, latency_ms=25)
        routed = router.route(category="image", project_id="film")
        self.assertTrue(routed["allowed"])
        self.assertEqual(routed["provider"], "fallback")
        self.assertTrue(routed["health_verified"])

    def test_paid_budget_routing_rejects_incompatible_cost_unit(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(self.connections, cost_policy=policy)
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            estimated_cost_unit="credits",
            paid_approved_providers={"primary", "fallback"},
            request_id="wrong-budget-unit",
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(
            result["decisions"][0]["status"],
            "COST_UNIT_NOT_SUPPORTED_FOR_BUDGET_POLICY",
        )

    def test_subscription_required_provider_needs_cost_policy_even_for_zero_estimate(self):
        self.connections.register_provider({
            "id": "paid-image", "name": "Paid Image", "category": "image",
            "auth_type": "NONE", "connection_type": "API",
            "enabled": True, "subscription_required": True,
        })
        self.connections.verify_connectivity("paid-image", lambda provider, **kwargs: {"ok": True})
        router = ProviderRoutingManager(self.connections)
        result = router.route(
            category="image", project_id="film", estimated_cost=0.0,
            excluded_providers={"primary", "fallback"},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["decisions"][0]["status"], "COST_POLICY_REQUIRED")

    def test_positive_cost_route_requires_cost_policy_or_verified_credits(self):
        router = ProviderRoutingManager(self.connections)
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        self.assertEqual(result["decisions"][0]["status"], "COST_POLICY_REQUIRED")

    def test_cost_policy_blocks_paid_route_without_budget_or_approval(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(self.connections, cost_policy=policy)
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")

    def test_verified_credit_manager_drives_free_first_routing(self):
        credits = ProviderCreditManager(self.root / "credits.json")
        credits.refresh(
            "fallback",
            lambda _: {
                "verified": True,
                "free_credits_remaining": 2.0,
                "source": "official-account-api",
                "unit": "USD",
            },
        )
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(
            self.connections, cost_policy=policy, credit_manager=credits
        )
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            request_id="verified-free-credit-job",
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["billing_mode"], "FREE_CREDITS")
        self.assertTrue(result["credit_reserved"])

    def test_quota_rejection_releases_new_free_credit_reservation(self):
        credits = ProviderCreditManager(self.root / "credits-quota.json")
        credits.refresh(
            "primary",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        quota = ProviderQuotaManager(
            self.root / "quota-credit-rollback.json",
            limits={"primary": {"hourly": 1}},
        )
        quota.reserve(provider_id="primary", request_id="already-used")
        router = ProviderRoutingManager(
            self.connections, quota_manager=quota, credit_manager=credits
        )
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            request_id="credit-rollback-job",
        )
        self.assertFalse(result["allowed"])
        self.assertNotIn("credit-rollback-job", credits.data["reservations"])
        self.assertEqual(credits.usable_free_credits(unit="USD"), {"primary": 5.0})

    def test_credit_balance_with_incompatible_unit_cannot_authorize_route(self):
        credits = ProviderCreditManager(self.root / "credits-units.json")
        credits.refresh(
            "fallback",
            lambda _: {
                "verified": True,
                "free_credits_remaining": 100.0,
                "source": "official-account-api",
                "unit": "credits",
            },
        )
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(
            self.connections, cost_policy=policy, credit_manager=credits
        )
        result = router.route(category="image", project_id="film", estimated_cost=1.0)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")
        matching_unit = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            estimated_cost_unit="credits", request_id="matching-credit-job",
        )
        self.assertTrue(matching_unit["allowed"])
        self.assertEqual(matching_unit["provider"], "fallback")
        self.assertEqual(matching_unit["billing_mode"], "FREE_CREDITS")
        zero_estimate = router.route(
            category="image", project_id="film", estimated_cost=0.0,
            request_id="zero-credit-estimate",
            excluded_providers={"primary"},
        )
        self.assertFalse(zero_estimate["allowed"])
        self.assertEqual(
            zero_estimate["decisions"][0]["status"],
            "ESTIMATED_COST_REQUIRED_FOR_CREDIT_PROVIDER",
        )

    def test_routing_rejects_unverified_credit_overrides(self):
        credits = ProviderCreditManager(self.root / "credits.json")
        router = ProviderRoutingManager(self.connections, credit_manager=credits)
        result = router.route(
            category="image", project_id="film", free_credits={"primary": 999.0}
        )
        self.assertEqual(result["status"], "UNVERIFIED_FREE_CREDIT_INPUT_REJECTED")

    def test_cost_policy_rejects_unverified_free_credit_map(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(self.connections, cost_policy=policy)
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            free_credits={"fallback": 2.0},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "VERIFIED_CREDIT_MANAGER_REQUIRED")

    def test_duplicate_quota_request_cannot_route_provider_twice(self):
        quota = ProviderQuotaManager(
            self.root / "duplicate-quota.json",
            limits={"primary": {"hourly": 3}, "fallback": {"hourly": 3}},
        )
        router = ProviderRoutingManager(self.connections, quota_manager=quota)
        first = router.route(category="image", project_id="film", request_id="duplicate-route")
        second = router.route(category="image", project_id="film", request_id="duplicate-route")
        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])
        self.assertEqual(second["status"], "REQUEST_ALREADY_RESERVED")
        self.assertEqual(quota.data["requests"]["duplicate-route"]["status"], "RESERVED")

    def test_duplicate_paid_reservation_cannot_start_second_provider_call(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(self.connections, cost_policy=policy)
        options = dict(
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary"}, request_id="duplicate-paid-route",
        )
        first = router.route(**options)
        second = router.route(**options)
        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])
        self.assertEqual(second["status"], "REQUEST_ALREADY_RESERVED")
        self.assertIn("duplicate-paid-route", policy.ledger["projects"]["film"]["reservations"])

    def test_duplicate_free_credit_reservation_cannot_start_second_provider_call(self):
        credits = ProviderCreditManager(self.root / "duplicate-credit.json")
        credits.refresh(
            "primary",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        router = ProviderRoutingManager(self.connections, credit_manager=credits)
        options = dict(
            category="image", project_id="film", estimated_cost=1.0,
            request_id="duplicate-credit-route",
        )
        first = router.route(**options)
        second = router.route(**options)
        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])
        self.assertEqual(second["status"], "REQUEST_ALREADY_RESERVED")
        self.assertEqual(
            credits.data["reservations"]["duplicate-credit-route"]["provider_id"],
            "primary",
        )

    def test_quota_manager_blocks_over_limit_provider_and_uses_fallback(self):
        quota = ProviderQuotaManager(
            self.root / "quotas.json",
            limits={"primary": {"hourly": 1}, "fallback": {"hourly": 1}},
        )
        router = ProviderRoutingManager(self.connections, quota_manager=quota)
        first = router.route(category="image", project_id="film", request_id="job-1")
        second = router.route(category="image", project_id="film", request_id="job-2")
        self.assertEqual(first["provider"], "primary")
        self.assertTrue(first["quota_reserved"])
        self.assertEqual(second["provider"], "fallback")
        self.assertTrue(second["quota_reserved"])

    def test_free_credit_fallback_requires_new_id_unless_unstarted_is_confirmed(self):
        credits = ProviderCreditManager(self.root / "credit-fallback.json")
        for provider_id in ("primary", "fallback"):
            credits.refresh(
                provider_id,
                lambda _: {
                    "verified": True, "free_credits_remaining": 5.0,
                    "unit": "USD", "source": "official-account-api",
                },
            )
        router = ProviderRoutingManager(self.connections, credit_manager=credits)
        first = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            request_id="credit-fallback-same-id",
        )
        self.assertEqual(first["provider"], "primary")
        blocked = router.select_fallback(
            failed_provider="primary", category="image", project_id="film",
            request_id="credit-fallback-same-id",
        )
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["status"], "NEW_FALLBACK_REQUEST_ID_REQUIRED")
        fallback = router.select_fallback(
            failed_provider="primary", release_reservations=True,
            category="image", project_id="film", estimated_cost=1.0,
            request_id="credit-fallback-same-id",
        )
        self.assertTrue(fallback["allowed"])
        self.assertEqual(fallback["provider"], "fallback")
        self.assertEqual(credits.data["reservations"]["credit-fallback-same-id"]["provider_id"], "fallback")

    def test_fallback_releases_failed_provider_quota_reservation(self):
        quota = ProviderQuotaManager(
            self.root / "quotas.json",
            limits={"primary": {"hourly": 2}, "fallback": {"hourly": 2}},
        )
        router = ProviderRoutingManager(self.connections, quota_manager=quota)
        router.route(category="image", project_id="film", request_id="job-1")
        fallback = router.select_fallback(
            failed_provider="primary",
            release_reservations=True,
            category="image",
            project_id="film",
            request_id="job-1",
        )
        self.assertTrue(fallback["allowed"])
        self.assertEqual(fallback["provider"], "fallback")
        self.assertEqual(quota.data["requests"]["job-1"]["provider_id"], "fallback")

    def test_quota_rejection_releases_new_cost_reservation(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        quota = ProviderQuotaManager(
            self.root / "quota-rollback.json",
            limits={"primary": {"hourly": 1}, "fallback": {"hourly": 1}},
        )
        quota.reserve(provider_id="primary", request_id="quota-blocker")
        router = ProviderRoutingManager(
            self.connections, cost_policy=policy, quota_manager=quota
        )

        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary"}, request_id="paid-job",
        )

        self.assertFalse(result["allowed"])
        reservations = policy.ledger["projects"]["film"]["reservations"]
        self.assertNotIn("paid-job", reservations)

    def test_already_reserved_quota_rolls_back_new_cost_reservation(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        quota = ProviderQuotaManager(
            self.root / "quota-already-reserved.json",
            limits={"primary": {"hourly": 2}, "fallback": {"hourly": 2}},
        )
        quota.reserve(provider_id="primary", request_id="paid-job")
        router = ProviderRoutingManager(
            self.connections, cost_policy=policy, quota_manager=quota
        )
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary"}, request_id="paid-job",
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "REQUEST_ALREADY_RESERVED")
        self.assertNotIn("paid-job", policy.ledger["projects"]["film"]["reservations"])
        self.assertEqual(result["reservation_cleanup"], [{"manager": "cost", "released": True}])

    def test_already_reserved_quota_rolls_back_new_credit_reservation(self):
        credits = ProviderCreditManager(self.root / "credits-already-reserved.json")
        credits.refresh(
            "primary",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        quota = ProviderQuotaManager(
            self.root / "quota-credit-already-reserved.json",
            limits={"primary": {"hourly": 2}},
        )
        quota.reserve(provider_id="primary", request_id="free-job")
        router = ProviderRoutingManager(
            self.connections, quota_manager=quota, credit_manager=credits
        )
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            request_id="free-job",
        )
        self.assertEqual(result["status"], "REQUEST_ALREADY_RESERVED")
        self.assertNotIn("free-job", credits.data["reservations"])
        self.assertEqual(credits.usable_free_credits(unit="USD"), {"primary": 5.0})
        self.assertEqual(result["reservation_cleanup"], [{"manager": "credit", "released": True}])

    def test_reservation_cleanup_failure_stops_routing(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        quota = ProviderQuotaManager(
            self.root / "quota-cleanup-failure.json",
            limits={"primary": {"hourly": 2}},
        )
        quota.reserve(provider_id="primary", request_id="paid-job")
        router = ProviderRoutingManager(
            self.connections, cost_policy=policy, quota_manager=quota
        )
        with patch.object(policy, "release_reservation", side_effect=OSError("storage unavailable")):
            result = router.route(
                category="image", project_id="film", estimated_cost=1.0,
                paid_approved_providers={"primary"}, request_id="paid-job",
            )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "RESERVATION_CLEANUP_FAILED")
        self.assertEqual(result["reservation_cleanup"][0]["status"], "CLEANUP_FAILED")
        self.assertIn("paid-job", policy.ledger["projects"]["film"]["reservations"])

    def test_quota_rejection_does_not_release_preexisting_cost_reservation(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        quota = ProviderQuotaManager(
            self.root / "quota-existing.json",
            limits={"primary": {"hourly": 1}, "fallback": {"hourly": 1}},
        )
        quota.reserve(provider_id="primary", request_id="quota-blocker")
        policy.reserve_usage(
            project_id="film", provider_id="primary",
            estimated_cost=1.0, request_id="existing-paid-job",
        )
        router = ProviderRoutingManager(
            self.connections, cost_policy=policy, quota_manager=quota
        )

        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary"}, request_id="existing-paid-job",
        )

        self.assertFalse(result["allowed"])
        reservation = policy.ledger["projects"]["film"]["reservations"]["existing-paid-job"]
        self.assertEqual(reservation["provider_id"], "primary")

    def test_paid_route_fails_closed_without_reservation_method(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(self.connections, cost_policy=policy)
        with patch.object(policy, "reserve_usage", None):
            result = router.route(
                category="image", project_id="film", estimated_cost=1.0,
                paid_approved_providers={"primary"}, request_id="no-reserver",
            )
        self.assertFalse(result["allowed"])
        self.assertTrue(any(
            decision.get("status") == "COST_RESERVATION_MANAGER_UNAVAILABLE"
            for decision in result["decisions"]
        ))
        project = policy.ledger["projects"].get("film", {})
        self.assertNotIn("no-reserver", project.get("reservations", {}))

    def test_paid_routing_reserves_budget_and_requires_idempotency_key(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(self.connections, cost_policy=policy)
        missing_id = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary"},
        )
        self.assertFalse(missing_id["allowed"])
        self.assertTrue(any(
            item["status"] == "COST_RESERVATION_REQUEST_ID_REQUIRED"
            for item in missing_id["decisions"]
        ))
        routed = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary"}, request_id="paid-job",
        )
        self.assertEqual(routed["provider"], "primary")
        self.assertTrue(routed["cost_reserved"])
        self.assertEqual(
            policy.ledger["projects"]["film"]["reservations"]["paid-job"]["provider_id"],
            "primary",
        )

    def test_fallback_releases_and_reassigns_budget_and_quota_reservations(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        quota = ProviderQuotaManager(
            self.root / "quotas.json",
            limits={"primary": {"hourly": 2}, "fallback": {"hourly": 2}},
        )
        router = ProviderRoutingManager(
            self.connections, cost_policy=policy, quota_manager=quota
        )
        first = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary", "fallback"}, request_id="job-1",
        )
        self.assertEqual(first["provider"], "primary")
        fallback = router.select_fallback(
            failed_provider="primary",
            release_reservations=True,
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary", "fallback"}, request_id="job-1",
        )
        self.assertEqual(fallback["provider"], "fallback")
        self.assertEqual(
            policy.ledger["projects"]["film"]["reservations"]["job-1"]["provider_id"],
            "fallback",
        )
        self.assertEqual(quota.data["requests"]["job-1"]["provider_id"], "fallback")

    def test_fallback_preserves_possible_charges_and_requires_new_attempt_id(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        quota = ProviderQuotaManager(
            self.root / "quotas.json",
            limits={"primary": {"hourly": 3}, "fallback": {"hourly": 3}},
        )
        router = ProviderRoutingManager(
            self.connections, cost_policy=policy, quota_manager=quota
        )
        router.route(
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary", "fallback"}, request_id="attempt-1",
        )
        same_id = router.select_fallback(
            failed_provider="primary",
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary", "fallback"}, request_id="attempt-1",
        )
        self.assertEqual(same_id["status"], "NEW_FALLBACK_REQUEST_ID_REQUIRED")
        self.assertEqual(
            policy.ledger["projects"]["film"]["reservations"]["attempt-1"]["provider_id"],
            "primary",
        )
        self.assertEqual(quota.data["requests"]["attempt-1"]["provider_id"], "primary")

        fallback = router.select_fallback(
            failed_provider="primary",
            failed_request_id="attempt-1",
            category="image", project_id="film", estimated_cost=1.0,
            paid_approved_providers={"primary", "fallback"}, request_id="attempt-2",
        )
        self.assertEqual(fallback["provider"], "fallback")
        self.assertEqual(
            policy.ledger["projects"]["film"]["reservations"]["attempt-1"]["provider_id"],
            "primary",
        )
        self.assertEqual(
            policy.ledger["projects"]["film"]["reservations"]["attempt-2"]["provider_id"],
            "fallback",
        )
        self.assertEqual(quota.data["requests"]["attempt-1"]["provider_id"], "primary")
        self.assertEqual(quota.data["requests"]["attempt-2"]["provider_id"], "fallback")

    def test_quota_enabled_routing_requires_request_id(self):
        quota = ProviderQuotaManager(
            self.root / "quotas.json", limits={"primary": {"daily": 2}}
        )
        router = ProviderRoutingManager(self.connections, quota_manager=quota)
        result = router.route(category="image", project_id="film")
        self.assertEqual(result["status"], "QUOTA_REQUEST_ID_REQUIRED")

    def test_invalid_route_inputs_and_missing_health_evidence_fail_closed(self):
        router = ProviderRoutingManager(self.connections)
        self.assertEqual(router.route(category="", project_id="film")["status"], "INVALID_ROUTE_REQUEST")
        self.assertEqual(router.route(category="image", project_id="film", strategy="MAGIC")["status"], "INVALID_ROUTING_STRATEGY")
        self.assertEqual(
            router.route(category="image", project_id="film", free_credits={"primary": float("nan")})["status"],
            "INVALID_FREE_CREDIT_MAP",
        )
        self.assertEqual(
            router.route(category="image", project_id="film", estimated_costs={"primary": float("inf")})["status"],
            "INVALID_COST_ESTIMATES",
        )
        self.assertEqual(
            router.route(category="image", project_id="film", require_health_evidence=True)["status"],
            "HEALTH_DATA_REQUIRED",
        )
        self.assertEqual(
            router.route(category="image", project_id="film", strategy="HEALTHIEST_FIRST")["status"],
            "HEALTH_DATA_REQUIRED",
        )
        self.assertEqual(
            router.select_fallback(failed_provider="primary")["status"],
            "INVALID_ROUTE_REQUEST",
        )


if __name__ == "__main__":
    unittest.main()
