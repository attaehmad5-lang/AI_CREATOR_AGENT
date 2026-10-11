import json
from concurrent.futures import ThreadPoolExecutor
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

    def test_finalization_is_idempotent_and_invalid_amounts_fail_closed(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0, request_id="job-repeat"
        )
        self.quota.reserve(provider_id="image", request_id="job-repeat")
        kwargs = dict(
            project_id="film", provider_id="image", request_id="job-repeat",
            estimated_cost=2.0, actual_cost=1.5, success=True, latency_ms=10,
        )
        first = self.service.finalize_started_request(**kwargs)
        second = self.service.finalize_started_request(**kwargs)
        self.assertTrue(first["finalized"])
        self.assertTrue(second["finalized"])
        self.assertEqual(second["cost"]["status"], "DUPLICATE_REQUEST")
        self.assertEqual(second["health"]["status"], "SKIPPED_DUPLICATE_FINALIZATION")
        self.assertEqual(len(self.health.data["providers"]["image"]["history"]), 1)
        conflict = self.service.finalize_started_request(**{**kwargs, "success": False})
        self.assertFalse(conflict["finalized"])
        self.assertEqual(conflict["cost"]["status"], "REQUEST_ID_CONFLICT")
        self.assertEqual(len(self.health.data["providers"]["image"]["history"]), 1)
        invalid = self.service.finalize_started_request(**{**kwargs, "actual_cost": float("nan")})
        self.assertEqual(invalid["status"], "INVALID_COST_AMOUNT")

    def test_concurrent_finalization_is_idempotent(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0,
            request_id="job-concurrent-finalize",
        )
        self.quota.reserve(provider_id="image", request_id="job-concurrent-finalize")
        kwargs = dict(
            project_id="film", provider_id="image", request_id="job-concurrent-finalize",
            estimated_cost=2.0, actual_cost=1.5, success=True, latency_ms=10,
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.service.finalize_started_request(**kwargs), range(12)))
        self.assertTrue(all(result["finalized"] for result in results))
        self.assertEqual(self.cost.ledger["projects"]["film"]["actual_spend"], 1.5)
        self.assertEqual(len(self.health.data["providers"]["image"]["history"]), 1)
        self.assertEqual(self.quota.data["requests"]["job-concurrent-finalize"]["status"], "COMMITTED")

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

    def test_failed_started_request_retains_cost_reservation_when_charge_is_unknown(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0, request_id="job-failed-unknown"
        )
        self.quota.reserve(provider_id="image", request_id="job-failed-unknown")
        result = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="job-failed-unknown",
            estimated_cost=2.0, actual_cost=0.0, success=False, error_code="TIMEOUT",
        )
        self.assertFalse(result["finalized"])
        self.assertEqual(result["cost"]["status"], "CHARGE_OUTCOME_UNKNOWN")
        self.assertIn("job-failed-unknown", self.cost.ledger["projects"]["film"]["reservations"])
        self.assertNotIn("job-failed-unknown", self.cost.ledger["projects"]["film"]["requests"])
        self.assertEqual(result["quota"]["status"], "COMMITTED")
        self.assertEqual(self.quota.data["requests"]["job-failed-unknown"]["status"], "COMMITTED")

    def test_failed_started_request_releases_cost_only_with_explicit_no_charge_confirmation(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0, request_id="job-failed-free"
        )
        self.quota.reserve(provider_id="image", request_id="job-failed-free")
        result = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="job-failed-free",
            estimated_cost=2.0, actual_cost=0.0, success=False,
            error_code="REJECTED_BEFORE_ACCEPTANCE", charge_outcome="not_incurred",
        )
        self.assertTrue(result["finalized"])
        self.assertEqual(result["cost"]["status"], "NO_CHARGE_RECORDED")
        project = self.cost.ledger["projects"]["film"]
        self.assertNotIn("job-failed-free", project["reservations"])
        self.assertEqual(project["requests"]["job-failed-free"]["charge_outcome"], "not_incurred")
        # A started request consumes quota even if the provider confirms no monetary charge.
        self.assertEqual(result["quota"]["status"], "COMMITTED")
        repeated = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="job-failed-free",
            estimated_cost=2.0, actual_cost=0.0, success=False,
            error_code="REJECTED_BEFORE_ACCEPTANCE", charge_outcome="not_incurred",
        )
        self.assertTrue(repeated["finalized"])
        self.assertEqual(repeated["cost"]["status"], "DUPLICATE_REQUEST")
        self.assertEqual(repeated["health"]["status"], "SKIPPED_DUPLICATE_FINALIZATION")
        self.assertEqual(len(self.health.data["providers"]["image"]["history"]), 1)

    def test_failed_started_request_with_reported_charge_reconciles_cost(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0, request_id="job-failed-charged"
        )
        self.quota.reserve(provider_id="image", request_id="job-failed-charged")
        result = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="job-failed-charged",
            estimated_cost=2.0, actual_cost=0.75, success=False, error_code="OUTPUT_FAILED",
        )
        self.assertTrue(result["finalized"])
        self.assertEqual(result["cost"]["status"], "RECONCILED")
        self.assertEqual(self.cost.ledger["projects"]["film"]["actual_spend"], 0.75)
        self.assertNotIn("job-failed-charged", self.cost.ledger["projects"]["film"]["reservations"])
        self.assertEqual(result["quota"]["status"], "COMMITTED")

    def test_invalid_charge_outcome_fails_closed(self):
        result = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="job-invalid-charge",
            estimated_cost=2.0, actual_cost=0.0, success=False, charge_outcome="maybe",
        )
        self.assertEqual(result["status"], "INVALID_CHARGE_OUTCOME")
        self.assertFalse(result["finalized"])
        malformed = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="job-malformed-charge",
            estimated_cost=2.0, actual_cost=0.0, success=False, charge_outcome=[],
        )
        self.assertEqual(malformed["status"], "INVALID_CHARGE_OUTCOME")

    def test_unavailable_optional_managers_fail_with_explicit_status(self):
        service = ProviderService(self.connections)
        self.assertEqual(service.route_request(category="image", project_id="film")["status"], "ROUTING_MANAGER_UNAVAILABLE")
        self.assertEqual(service.validate_output("missing.png", modality="image")["status"], "OUTPUT_VALIDATOR_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
