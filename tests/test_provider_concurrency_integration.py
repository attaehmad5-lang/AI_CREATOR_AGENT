import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.concurrency_manager import ProviderConcurrencyManager
from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.quota_manager import ProviderQuotaManager
from projects.provider_store.routing_manager import ProviderRoutingManager


class ProviderConcurrencyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        catalog = self.root / "providers.json"
        state = self.root / "connections.json"
        catalog.write_text(json.dumps({
            "providers": [{
                "id": "image",
                "name": "Image",
                "category": "image",
                "auth_type": "NONE",
                "connection_type": "API",
                "enabled": True,
                "zero_cost_allowed": True,
            }]
        }), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(catalog, state)
        verified = self.connections.verify_connectivity(
            "image", lambda provider, **kwargs: {"ok": True}
        )
        self.assertIs(verified.get("verified"), True)
        self.concurrency = ProviderConcurrencyManager(
            state.with_name("provider_concurrency.json"),
            default_provider_limit=2,
            project_limit=4,
            global_limit=8,
        )
        self.cost = ProviderCostPolicy(
            self.root / "cost.json", project_budgets={"film": 10.0}
        )
        self.quota = ProviderQuotaManager(
            self.root / "quota.json", limits={"image": {"daily": 20}}
        )
        self.router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        self.service = ProviderService(
            self.connections,
            routing_manager=self.router,
            cost_policy=self.cost,
            quota_manager=self.quota,
            concurrency_manager=self.concurrency,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_provider_limit_blocks_adapter_before_invocation(self):
        configured = self.service.set_provider_concurrency_limit("image", 1)
        self.assertEqual(configured.get("status"), "LIMIT_UPDATED")
        held = self.concurrency.acquire(
            provider_id="image", project_id="film", request_id="already-running"
        )
        self.assertTrue(held.get("acquired"))
        calls = []
        result = self.service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="blocked-by-concurrency",
            estimated_cost=0.0,
            adapter=lambda **kwargs: calls.append(kwargs) or {
                "started": False, "success": False,
            },
            modality="image",
        )
        self.assertEqual(result.get("status"), "PROVIDER_CONCURRENCY_LIMIT_REACHED")
        self.assertEqual(calls, [])
        self.assertEqual(self.cost.ledger["projects"]["film"]["reservations"], {})
        self.assertEqual(
            self.quota.data["requests"]["blocked-by-concurrency"]["status"],
            "RELEASED",
        )

    def test_known_terminal_adapter_result_releases_capacity(self):
        result = self.service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="completed-request",
            estimated_cost=0.0,
            adapter=lambda **kwargs: {
                "started": True,
                "success": False,
                "actual_cost": 0.0,
                "charge_outcome": "not_incurred",
                "credit_outcome": "not_used",
            },
            modality="image",
        )
        self.assertEqual(result.get("status"), "EXECUTION_FAILED")
        self.assertTrue(result.get("concurrency_lease_released"))
        lease = self.concurrency.get("completed-request")
        self.assertEqual(lease["lease"]["status"], "COMPLETED")

    def test_ambiguous_adapter_exception_retains_capacity(self):
        def adapter(**kwargs):
            raise TimeoutError("private endpoint response must not escape")
        result = self.service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="unknown-request",
            estimated_cost=0.0,
            adapter=adapter,
            modality="image",
        )
        self.assertEqual(result.get("status"), "EXECUTION_OUTCOME_UNKNOWN")
        self.assertTrue(result.get("concurrency_lease_retained"))
        self.assertNotIn("private endpoint", repr(result))
        lease = self.concurrency.get("unknown-request")
        self.assertEqual(lease["lease"]["status"], "UNKNOWN")
        self.assertEqual(self.concurrency.summary()["UNKNOWN"], 1)

    def test_admin_permission_is_required_to_change_limits_and_reconcile(self):
        authorization = ProviderAuthorizationManager(self.root / "roles.json")
        authorization.assign_role("admin-1", "admin")
        authorization.assign_role("operator-1", "operator")
        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
            concurrency_manager=self.concurrency,
        )
        denied = service.set_provider_concurrency_limit(
            "image", 1, principal_id="operator-1"
        )
        self.assertEqual(denied.get("status"), "AUTHORIZATION_DENIED")
        allowed = service.set_provider_concurrency_limit(
            "image", 1, principal_id="admin-1"
        )
        self.assertEqual(allowed.get("status"), "LIMIT_UPDATED")

        self.concurrency.acquire(
            provider_id="image", project_id="film", request_id="ambiguous-admin-lease"
        )
        self.concurrency.mark_started(lease_id="ambiguous-admin-lease")
        self.concurrency.release(
            lease_id="ambiguous-admin-lease", outcome="unknown"
        )
        denied_reconcile = service.reconcile_provider_concurrency_lease(
            "ambiguous-admin-lease",
            outcome="completed",
            confirmed=True,
            principal_id="operator-1",
        )
        self.assertEqual(denied_reconcile.get("status"), "AUTHORIZATION_DENIED")
        reconciled = service.reconcile_provider_concurrency_lease(
            "ambiguous-admin-lease",
            outcome="completed",
            confirmed=True,
            principal_id="admin-1",
        )
        self.assertEqual(reconciled.get("status"), "LEASE_RECONCILED")
        self.assertEqual(
            self.concurrency.get("ambiguous-admin-lease")["lease"]["status"],
            "RECONCILED",
        )

    def test_reserved_lease_requires_confirmed_not_running_reconciliation(self):
        self.concurrency.acquire(
            provider_id="image", project_id="film", request_id="reserved-lease"
        )
        authorization = ProviderAuthorizationManager(self.root / "reserved-roles.json")
        authorization.assign_role("admin-1", "admin")
        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
            concurrency_manager=self.concurrency,
        )
        denied = service.reconcile_provider_concurrency_lease(
            "reserved-lease",
            outcome="completed",
            confirmed=True,
            principal_id="admin-1",
        )
        self.assertEqual(denied.get("status"), "CONFIRMED_NOT_STARTED_REQUIRED")
        released = service.reconcile_provider_concurrency_lease(
            "reserved-lease",
            outcome="not_running",
            confirmed=True,
            principal_id="admin-1",
        )
        self.assertEqual(released.get("status"), "LEASE_RELEASED")
        self.assertEqual(
            self.concurrency.get("reserved-lease")["lease"]["status"],
            "NOT_STARTED",
        )


if __name__ == "__main__":
    unittest.main()
