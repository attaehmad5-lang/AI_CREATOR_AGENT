import json
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.credit_manager import ProviderCreditManager
from projects.provider_store.routing_manager import ProviderRoutingManager
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

    def test_successful_zero_cost_report_needs_explicit_no_charge_confirmation(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0,
            request_id="successful-charge-unknown",
        )
        self.quota.reserve(provider_id="image", request_id="successful-charge-unknown")
        unknown = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="successful-charge-unknown",
            estimated_cost=2.0, actual_cost=0.0, success=True,
        )
        self.assertFalse(unknown["finalized"])
        self.assertEqual(unknown["cost"]["status"], "CHARGE_OUTCOME_UNKNOWN")
        self.assertIn("successful-charge-unknown", self.cost.ledger["projects"]["film"]["reservations"])
        self.assertEqual(unknown["quota"]["status"], "COMMITTED")

        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0,
            request_id="successful-confirmed-free",
        )
        self.quota.reserve(provider_id="image", request_id="successful-confirmed-free")
        confirmed = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="successful-confirmed-free",
            estimated_cost=2.0, actual_cost=0.0, success=True, charge_outcome="not_incurred",
        )
        self.assertTrue(confirmed["finalized"])
        self.assertEqual(confirmed["cost"]["status"], "NO_CHARGE_RECORDED")
        repeated = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="successful-confirmed-free",
            estimated_cost=2.0, actual_cost=0.0, success=True, charge_outcome="not_incurred",
        )
        self.assertTrue(repeated["finalized"])
        self.assertEqual(repeated["cost"]["status"], "DUPLICATE_REQUEST")

    def test_actual_cost_cannot_conflict_with_not_incurred_charge_outcome(self):
        result = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="contradictory-charge",
            estimated_cost=2.0, actual_cost=1.0, success=True, charge_outcome="not_incurred",
        )
        self.assertFalse(result["finalized"])
        self.assertEqual(result["status"], "INCONSISTENT_CHARGE_OUTCOME")

    def test_free_credit_route_reserves_and_finalizes_verified_credits(self):
        credits = ProviderCreditManager(self.root / "service-credits.json")
        credits.refresh(
            "image",
            lambda _: {
                "verified": True,
                "free_credits_remaining": 5.0,
                "unit": "USD",
                "source": "official-account-api",
            },
        )
        router = ProviderRoutingManager(self.connections, credit_manager=credits)
        service = ProviderService(
            self.connections,
            routing_manager=router,
            health_manager=self.health,
            credit_manager=credits,
        )
        route = service.route_request(
            category="image", project_id="film", estimated_cost=1.0,
            request_id="service-credit-job",
        )
        self.assertTrue(route["allowed"])
        self.assertTrue(route["credit_reserved"])
        self.assertEqual(credits.usable_free_credits(unit="USD"), {"image": 4.0})
        finalized = service.finalize_started_request(
            project_id="film", provider_id="image", request_id="service-credit-job",
            estimated_cost=1.0, actual_cost=0.0, success=True,
            billing_mode="FREE_CREDITS", credit_outcome="used",
            actual_credit_amount=1.0,
        )
        self.assertTrue(finalized["finalized"])
        self.assertEqual(finalized["credits"]["status"], "COMMITTED")
        self.assertEqual(credits.usable_free_credits(unit="USD"), {"image": 4.0})
        unstarted_route = service.route_request(
            category="image", project_id="film", estimated_cost=1.0,
            request_id="unstarted-credit-job",
        )
        self.assertTrue(unstarted_route["allowed"])
        released = service.release_unstarted_request(
            project_id="film", provider_id="image", request_id="unstarted-credit-job"
        )
        self.assertTrue(released["released"])
        self.assertEqual(credits.usable_free_credits(unit="USD"), {"image": 4.0})

    def test_concurrent_free_credit_finalization_commits_once(self):
        credits = ProviderCreditManager(self.root / "concurrent-service-credits.json")
        credits.refresh(
            "image",
            lambda _: {
                "verified": True, "free_credits_remaining": 5.0,
                "unit": "USD", "source": "official-account-api",
            },
        )
        router = ProviderRoutingManager(self.connections, credit_manager=credits)
        service = ProviderService(
            self.connections, routing_manager=router,
            health_manager=self.health, credit_manager=credits,
        )
        route = service.route_request(
            category="image", project_id="film", estimated_cost=1.0,
            request_id="concurrent-credit-finalize",
        )
        self.assertTrue(route["allowed"])
        kwargs = dict(
            project_id="film", provider_id="image", request_id="concurrent-credit-finalize",
            estimated_cost=1.0, actual_cost=0.0, success=True,
            billing_mode="FREE_CREDITS", credit_outcome="used",
            actual_credit_amount=1.0,
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: service.finalize_started_request(**kwargs), range(12)))
        self.assertTrue(all(result["finalized"] for result in results))
        self.assertEqual(credits.usable_free_credits(unit="USD"), {"image": 4.0})
        self.assertEqual(len(self.health.data["providers"]["image"]["history"]), 1)

    def test_execute_routed_request_validates_output_and_finalizes_usage(self):
        router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        service = ProviderService(
            self.connections, routing_manager=router, health_manager=self.health,
            retry_policy=self.retry, output_validator=self.validator,
            cost_policy=self.cost, quota_manager=self.quota,
        )
        output_path = self.root / "generated.png"
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-test-image")
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="execute-success",
            estimated_cost=0.0, output_path=str(output_path),
            adapter=lambda **kwargs: {
                "started": True, "success": True, "actual_cost": 0.0,
                "charge_outcome": "not_incurred", "output_path": str(output_path),
                "latency_ms": 5.0,
            },
            modality="image",
        )
        self.assertEqual(result["status"], "EXECUTION_SUCCEEDED")
        self.assertTrue(result["output"]["valid"])
        self.assertTrue(result["finalization"]["finalized"])
        self.assertEqual(result["provider"], "image")

    def test_execute_routed_request_releases_only_confirmed_unstarted_calls(self):
        router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        service = ProviderService(
            self.connections, routing_manager=router, health_manager=self.health,
            cost_policy=self.cost, quota_manager=self.quota,
        )
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="execute-not-started",
            estimated_cost=1.0, paid_approved_providers={"image"},
            adapter=lambda **kwargs: {"started": False, "success": False},
            modality="image",
        )
        self.assertEqual(result["status"], "EXECUTION_NOT_STARTED")
        self.assertTrue(result["release"]["released"])
        self.assertNotIn("execute-not-started", self.cost.ledger["projects"]["film"]["reservations"])
        self.assertNotIn("execute-not-started", self.quota.data["requests"])

    def test_execute_routed_request_rejects_contradictory_not_started_result(self):
        router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        service = ProviderService(
            self.connections, routing_manager=router,
            cost_policy=self.cost, quota_manager=self.quota,
        )
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="execute-contradictory-start",
            estimated_cost=1.0, paid_approved_providers={"image"},
            adapter=lambda **kwargs: {
                "started": False, "success": True, "actual_cost": 1.0,
                "charge_outcome": "incurred",
            },
            modality="image",
        )
        self.assertEqual(result["status"], "INCONSISTENT_START_RESULT")
        self.assertTrue(result["reservations_retained"])
        self.assertIn(
            "execute-contradictory-start",
            self.cost.ledger["projects"]["film"]["reservations"],
        )

    def test_execute_routed_request_retains_reservations_after_ambiguous_adapter_error(self):
        router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        service = ProviderService(
            self.connections, routing_manager=router, health_manager=self.health,
            cost_policy=self.cost, quota_manager=self.quota,
        )
        def adapter(**kwargs):
            raise TimeoutError("secret endpoint details")
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="execute-unknown",
            estimated_cost=1.0, paid_approved_providers={"image"},
            adapter=adapter, modality="image",
        )
        self.assertEqual(result["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertTrue(result["reservations_retained"])
        self.assertIn("execute-unknown", self.cost.ledger["projects"]["film"]["reservations"])
        self.assertEqual(self.quota.data["requests"]["execute-unknown"]["status"], "RESERVED")
        self.assertNotIn("secret endpoint details", repr(result))

    def test_execute_routed_request_keeps_reservations_on_invalid_cost_report(self):
        router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        service = ProviderService(
            self.connections, routing_manager=router, health_manager=self.health,
            output_validator=self.validator, cost_policy=self.cost, quota_manager=self.quota,
        )
        output_path = self.root / "cost-report.png"
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-test-image")
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="execute-invalid-cost",
            estimated_cost=1.0, paid_approved_providers={"image"},
            adapter=lambda **kwargs: {
                "started": True, "success": True, "actual_cost": float("nan"),
                "charge_outcome": "not_incurred", "output_path": str(output_path),
            },
            modality="image",
        )
        self.assertEqual(result["status"], "EXECUTION_ACCOUNTING_REQUIRED")
        self.assertIn("execute-invalid-cost", self.cost.ledger["projects"]["film"]["reservations"])
        self.assertEqual(self.quota.data["requests"]["execute-invalid-cost"]["status"], "COMMITTED")

    def test_execute_routed_request_rejects_invalid_output_but_accounts_for_started_call(self):
        router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        service = ProviderService(
            self.connections, routing_manager=router, health_manager=self.health,
            output_validator=self.validator, cost_policy=self.cost, quota_manager=self.quota,
        )
        output_path = self.root / "not-image.png"
        output_path.write_text("not an image", encoding="utf-8")
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="execute-bad-output",
            estimated_cost=0.0, adapter=lambda **kwargs: {
                "started": True, "success": True, "actual_cost": 0.0,
                "charge_outcome": "not_incurred", "output_path": str(output_path),
            },
            modality="image",
        )
        self.assertEqual(result["status"], "OUTPUT_VALIDATION_FAILED")
        self.assertFalse(result["output"]["valid"])
        self.assertTrue(result["finalization"]["finalized"])
        self.assertEqual(self.quota.data["requests"]["execute-bad-output"]["status"], "COMMITTED")

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
        malformed_billing = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="job-malformed-billing",
            estimated_cost=2.0, actual_cost=0.0, success=False, billing_mode=[],
        )
        self.assertEqual(malformed_billing["status"], "INVALID_BILLING_MODE")

    def test_unavailable_optional_managers_fail_with_explicit_status(self):
        service = ProviderService(self.connections)
        self.assertEqual(service.route_request(category="image", project_id="film")["status"], "ROUTING_MANAGER_UNAVAILABLE")
        self.assertEqual(service.validate_output("missing.png", modality="image")["status"], "OUTPUT_VALIDATOR_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
