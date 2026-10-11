import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.output_validator import ProviderOutputValidator
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.quota_manager import ProviderQuotaManager
from projects.provider_store.retry_policy import RetryPolicy


class ProviderServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog_path = self.root / "providers.json"
        self.state_path = self.root / "connections.json"
        self.catalog_path.write_text(json.dumps({
            "providers": [{
                "id": "image", "name": "Image", "category": "image",
                "auth_type": "NONE", "connection_type": "API", "enabled": True,
            }]
        }), encoding="utf-8")
        self.state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(self.catalog_path, self.state_path)
        self.health = ProviderHealthManager(self.root / "health.json")
        self.cost = ProviderCostPolicy(self.root / "cost.json", project_budgets={"film": 10.0})
        self.quota = ProviderQuotaManager(self.root / "quota.json", limits={"image": {"daily": 10}})
        self.validator = ProviderOutputValidator()
        self.retry = RetryPolicy(random_fn=lambda: 0)
        self.service = ProviderService(
            self.connections,
            health_manager=self.health,
            retry_policy=self.retry,
            output_validator=self.validator,
            cost_policy=self.cost,
            quota_manager=self.quota,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_verify_provider_updates_connection_and_health_together(self):
        result = self.service.verify_provider("image", lambda provider, timeout_seconds: True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["connection"]["status"], "CONNECTED")
        self.assertEqual(result["health"]["status"], "HEALTHY")
        self.assertEqual(self.connections.get_state("image")["status"], "CONNECTED")

    def test_failed_verification_updates_health_without_leaking_error_text(self):
        def probe(provider, timeout_seconds):
            raise ConnectionError("private endpoint and token")
        result = self.service.verify_provider("image", probe)
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "NETWORK_ERROR")
        self.assertNotIn("private endpoint and token", repr(result))
        self.assertEqual(self.health.data["providers"]["image"]["status"], "DEGRADED")

    def test_output_validation_and_retry_decision_are_delegated(self):
        path = self.root / "script.txt"
        path.write_text("script", encoding="utf-8")
        self.assertTrue(self.service.validate_output(path, modality="text")["valid"])
        retry = self.service.retry_decision(attempt=1, status_code=503)
        self.assertTrue(retry["retry"])
        self.assertEqual(retry["reason"], "TRANSIENT_HTTP_FAILURE")

    def test_started_request_reconciles_cost_commits_quota_and_records_health(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0, request_id="job-1"
        )
        self.quota.reserve(provider_id="image", request_id="job-1")
        result = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="job-1",
            estimated_cost=2.0, actual_cost=1.5, success=True, latency_ms=30,
        )
        self.assertTrue(result["finalized"])
        self.assertEqual(result["cost"]["status"], "RECONCILED")
        self.assertEqual(result["quota"]["status"], "COMMITTED")
        self.assertEqual(self.cost.ledger["projects"]["film"]["actual_spend"], 1.5)
        self.assertEqual(self.quota.data["requests"]["job-1"]["status"], "COMMITTED")

    def test_unstarted_request_releases_reservations_only_when_explicitly_called(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0, request_id="job-2"
        )
        self.quota.reserve(provider_id="image", request_id="job-2")
        result = self.service.release_unstarted_request(
            project_id="film", provider_id="image", request_id="job-2"
        )
        self.assertTrue(result["released"])
        self.assertNotIn("job-2", self.cost.ledger["projects"]["film"]["reservations"])
        self.assertNotIn("job-2", self.quota.data["requests"])

    def test_unavailable_optional_managers_fail_with_explicit_status(self):
        service = ProviderService(self.connections)
        self.assertEqual(service.route_request(category="image", project_id="film")["status"], "ROUTING_MANAGER_UNAVAILABLE")
        self.assertEqual(service.validate_output("missing.png", modality="image")["status"], "OUTPUT_VALIDATOR_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
