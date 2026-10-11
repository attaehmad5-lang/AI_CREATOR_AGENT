import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.routing_manager import ProviderRoutingManager
from projects.provider_store.quota_manager import ProviderQuotaManager
from projects.provider_store.credit_manager import ProviderCreditManager


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
        result = router.route(category="image", project_id="film", estimated_cost=1.0)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "fallback")
        self.assertEqual(result["billing_mode"], "FREE_CREDITS")

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
