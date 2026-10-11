import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.routing_manager import ProviderRoutingManager


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
                 "connection_type": "API", "enabled": True, "priority": 1, "capabilities": ["image"]},
                {"id": "fallback", "name": "Fallback", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "priority": 2, "capabilities": ["image"]},
                {"id": "disabled", "name": "Disabled", "category": "image", "auth_type": "NONE",
                 "connection_type": "API", "enabled": False, "priority": 0, "capabilities": ["image"]},
                {"id": "voice", "name": "Voice", "category": "voice", "auth_type": "NONE",
                 "connection_type": "API", "enabled": True, "priority": 1, "capabilities": ["voice"]},
            ]
        }), encoding="utf-8")
        self.state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(self.catalog_path, self.state_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_primary_route_skips_disabled_and_orders_by_priority(self):
        router = ProviderRoutingManager(self.connections)
        result = router.route(category="image", project_id="film")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "primary")
        self.assertFalse(result["health_verified"])

    def test_fallback_excludes_failed_provider(self):
        router = ProviderRoutingManager(self.connections)
        result = router.select_fallback(
            failed_provider="primary", category="image", project_id="film"
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["failed_provider"], "primary")

    def test_free_credit_and_lowest_cost_strategies(self):
        router = ProviderRoutingManager(self.connections)
        free_first = router.route(
            category="image", project_id="film", strategy="FREE_CREDITS_FIRST",
            free_credits={"primary": 0.0, "fallback": 2.0},
        )
        self.assertEqual(free_first["provider"], "fallback")
        cheapest = router.route(
            category="image", project_id="film", strategy="LOWEST_COST",
            estimated_costs={"primary": 1.5, "fallback": 0.2},
        )
        self.assertEqual(cheapest["provider"], "fallback")

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

    def test_cost_policy_blocks_paid_route_without_budget_or_approval(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(self.connections, cost_policy=policy)
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "NO_POLICY_COMPLIANT_PROVIDER")

    def test_cost_policy_selects_free_credit_provider_before_paid_fallback(self):
        policy = ProviderCostPolicy(project_budgets={"film": 5.0})
        router = ProviderRoutingManager(self.connections, cost_policy=policy)
        result = router.route(
            category="image", project_id="film", estimated_cost=1.0,
            free_credits={"fallback": 2.0},
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["billing_mode"], "FREE_CREDITS")

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


if __name__ == "__main__":
    unittest.main()
