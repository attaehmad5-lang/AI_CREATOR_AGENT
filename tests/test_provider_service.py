import json
import io
import logging
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_feature_flags import ProviderFeatureFlagManager
from projects.provider_store.provider_incident_manager import ProviderIncidentManager
from projects.provider_store.provider_benchmark_manager import ProviderBenchmarkManager
from projects.provider_store.provider_quality_policy import ProviderOutputQualityPolicy
from projects.provider_store.provider_metrics_manager import ProviderMetricsManager
from projects.provider_store.provider_job_manager import ProviderJobManager
from projects.provider_store.provider_data_policy import ProviderDataHandlingPolicy
from projects.provider_store.sensitive_data_policy import SensitiveDataPolicy
from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.budget_alert_manager import BudgetAlertManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.credit_manager import ProviderCreditManager
from projects.provider_store.routing_manager import ProviderRoutingManager
from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.output_validator import ProviderOutputValidator
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.provider_structured_logger import ProviderStructuredLogger
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
                "zero_cost_allowed": True,
            }]
        }), encoding="utf-8")
        self.state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(self.catalog_path, self.state_path)
        verified = self.connections.verify_connectivity(
            "image", lambda provider, **kwargs: {"ok": True}
        )
        if verified.get("verified") is not True:
            raise AssertionError("test fixture provider failed stub verification")
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

    def test_provider_verification_emits_secret_safe_structured_event(self):
        stream = io.StringIO()
        logger = logging.Logger("provider-service-test", level=logging.DEBUG)
        logger.addHandler(logging.StreamHandler(stream))
        service = ProviderService(
            self.connections,
            health_manager=self.health,
            structured_logger=ProviderStructuredLogger(logger),
        )
        result = service.verify_provider(
            "image",
            lambda provider, timeout_seconds: True,
            environ={"API_TOKEN": "never-log-this-token"},
        )
        self.assertTrue(result["verified"])
        encoded = stream.getvalue()
        event = json.loads(encoded)
        self.assertEqual(event["event"], "provider.verify")
        self.assertEqual(event["provider_id"], "image")
        self.assertEqual(event["outcome"], "OK")
        self.assertNotIn("never-log-this-token", encoded)
        self.assertNotIn("API_TOKEN", encoded)

    def test_builtin_http_provider_verification_updates_connection_and_health(self):
        class Response:
            status = 200
            closed = False

            def getcode(self):
                return self.status

            def read(self, size=-1):
                return b'{"ok": true}'[:size]

            def close(self):
                self.closed = True

        class Opener:
            def __init__(self):
                self.response = Response()
                self.calls = []

            def open(self, request, timeout):
                self.calls.append((request, timeout))
                return self.response

        catalog = self.root / "http-providers.json"
        state = self.root / "http-connections.json"
        catalog.write_text(json.dumps({"providers": [{
            "id": "huggingface", "name": "Hugging Face", "category": "image",
            "auth_type": "API_TOKEN", "connection_type": "API", "enabled": True,
            "token_env": "HF_TOKEN", "allowed_hosts": ["api.example.com"],
        }]}), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        manager = ConnectionManager(catalog, state)
        self.assertTrue(manager.configure_provider(
            "huggingface", endpoint="https://api.example.com/health",
            authentication_required=True,
        )["saved"])
        health = ProviderHealthManager(self.root / "http-health.json")
        service = ProviderService(manager, health_manager=health)
        opener = Opener()
        result = service.verify_http_provider(
            "huggingface", environ={"HF_TOKEN": "do-not-persist"},
            opener=opener, timeout_seconds=1.5,
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "CONNECTED")
        self.assertEqual(result["health"]["status"], "HEALTHY")
        self.assertEqual(opener.calls[0][1], 1.5)
        self.assertEqual(
            opener.calls[0][0].get_header("Authorization"), "Bearer do-not-persist"
        )
        self.assertTrue(opener.response.closed)
        self.assertNotIn("do-not-persist", state.read_text(encoding="utf-8"))
        self.assertNotIn("do-not-persist", repr(result))

    def test_oauth_access_token_verifies_connection_without_persisting_token(self):
        class Response:
            status = 200
            closed = False
            def getcode(self):
                return self.status
            def read(self, size=-1):
                return b'{"ok": true}'[:size]
            def close(self):
                self.closed = True

        class Opener:
            def __init__(self):
                self.response = Response()
                self.calls = []
            def open(self, request, timeout):
                self.calls.append((request, timeout))
                return self.response

        class OAuthStub:
            def get_access_token(self, provider_id):
                return "oauth-access-secret"

        catalog = self.root / "oauth-providers.json"
        state = self.root / "oauth-connections.json"
        health_path = self.root / "oauth-health.json"
        catalog.write_text(json.dumps({"providers": [{
            "id": "oauth_image", "name": "OAuth Image", "category": "image",
            "auth_type": "API_TOKEN", "connection_type": "API", "enabled": True,
            "token_env": "OAUTH_ACCESS_TOKEN", "allowed_hosts": ["api.example.com"],
        }]}), encoding="utf-8")
        state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        manager = ConnectionManager(catalog, state)
        configured = manager.configure_provider(
            "oauth_image", endpoint="https://api.example.com/generate",
            probe_endpoint="https://api.example.com/health",
            authentication_required=True,
        )
        self.assertTrue(configured["saved"])
        health = ProviderHealthManager(health_path)
        service = ProviderService(manager, health_manager=health)
        opener = Opener()
        result = service.verify_oauth_provider("oauth_image", OAuthStub(), opener=opener)
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "CONNECTED")
        self.assertEqual(opener.calls[0][0].get_header("Authorization"), "Bearer oauth-access-secret")
        self.assertEqual(opener.calls[0][0].full_url, "https://api.example.com/health")
        self.assertEqual(manager.get_provider_configuration("oauth_image")["configuration"]["endpoint"], "https://api.example.com/generate")
        self.assertTrue(opener.response.closed)
        self.assertNotIn("oauth-access-secret", state.read_text(encoding="utf-8"))
        self.assertNotIn("oauth-access-secret", health_path.read_text(encoding="utf-8"))
        self.assertNotIn("oauth-access-secret", repr(result))

    def test_oauth_verification_fails_closed_when_provider_lookup_raises(self):
        class OAuthStub:
            def get_access_token(self, provider_id):
                return "temporary-token"

        class BrokenConnectionManager:
            def verify_connectivity(self, *args, **kwargs):
                raise AssertionError("connectivity must not run after lookup failure")
            def get_provider(self, provider_id):
                raise OSError("private storage detail")

        result = ProviderService(BrokenConnectionManager()).verify_oauth_provider(
            "provider", OAuthStub()
        )
        self.assertEqual(result["status"], "PROVIDER_LOOKUP_FAILED")
        self.assertEqual(result["error_type"], "OSError")
        self.assertNotIn("private storage detail", repr(result))
        self.assertNotIn("temporary-token", repr(result))

    def test_oauth_verification_fails_closed_without_unexpired_access_token(self):
        class OAuthStub:
            def get_access_token(self, provider_id):
                return None
        class ConnectionStub:
            def verify_connectivity(self, *args, **kwargs):
                raise AssertionError("HTTP verification must not run without a valid OAuth token")
            def get_provider(self, provider_id):
                return {"id": provider_id, "token_env": "OAUTH_ACCESS_TOKEN"}
            def verify_connectivity(self, provider_id, probe, **kwargs):
                return {"status": "CONNECTED", "provider": provider_id, "verified": True}
        service = ProviderService(ConnectionStub())
        result = service.verify_oauth_provider("image", OAuthStub())
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "OAUTH_ACCESS_TOKEN_UNAVAILABLE")

    def test_builtin_http_verification_rejects_invalid_probe_configuration(self):
        result = self.service.verify_http_provider(
            "image", max_response_bytes=-1
        )
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "HTTP_PROBE_CONFIGURATION_INVALID")

    def test_manual_provider_verification_records_health_without_session_leak(self):
        registered = self.connections.register_provider({
            "id": "manual-music", "name": "Manual Music", "category": "music",
            "auth_type": "ACCOUNT", "connection_type": "BROWSER_SESSION", "enabled": True,
        })
        self.assertTrue(registered["saved"])
        session = {"access_token": "manual-session-private"}
        result = self.service.verify_manual_provider(
            "manual-music", session,
            lambda provider, *, session, timeout_seconds: {"ok": session.get("access_token") == "manual-session-private"},
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "CONNECTED")
        self.assertEqual(result["health"]["status"], "HEALTHY")
        self.assertNotIn("manual-session-private", repr(result))
        self.assertNotIn("manual-session-private", self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("manual-session-private", (self.root / "health.json").read_text(encoding="utf-8"))

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

        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0,
            request_id="job-no-charge-replay",
        )
        self.quota.reserve(provider_id="image", request_id="job-no-charge-replay")
        no_charge_kwargs = {
            "project_id": "film", "provider_id": "image",
            "request_id": "job-no-charge-replay", "estimated_cost": 2.0,
            "actual_cost": 0.0, "success": False, "charge_outcome": "not_incurred",
        }
        no_charge_first = self.service.finalize_started_request(**no_charge_kwargs)
        no_charge_conflict = self.service.finalize_started_request(
            **{**no_charge_kwargs, "charge_outcome": "unknown"}
        )
        self.assertTrue(no_charge_first["finalized"])
        self.assertFalse(no_charge_conflict["finalized"])
        self.assertEqual(no_charge_conflict["cost"]["status"], "REQUEST_ID_CONFLICT")

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
        health_count_before_confirmation = len(self.health.data["providers"]["image"]["history"])
        confirmed_after_unknown = self.service.finalize_started_request(
            project_id="film", provider_id="image", request_id="successful-charge-unknown",
            estimated_cost=2.0, actual_cost=0.0, success=True, charge_outcome="not_incurred",
        )
        self.assertTrue(confirmed_after_unknown["finalized"])
        self.assertEqual(
            len(self.health.data["providers"]["image"]["history"]),
            health_count_before_confirmation,
        )
        self.assertNotIn(
            "successful-charge-unknown",
            self.cost.ledger["projects"]["film"]["reservations"],
        )

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
        self.assertEqual(self.quota.data["requests"]["execute-not-started"]["status"], "RELEASED")
        self.assertEqual(self.quota.reserve(provider_id="image", request_id="execute-not-started")["status"], "ALREADY_RELEASED")

    def test_execute_failure_uses_supported_retry_metadata_and_requires_idempotency(self):
        router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        service = ProviderService(
            self.connections, routing_manager=router, health_manager=self.health,
            retry_policy=self.retry, cost_policy=self.cost, quota_manager=self.quota,
        )
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="retryable-provider-failure",
            estimated_cost=0.0,
            adapter=lambda **kwargs: {
                "started": True, "success": False, "actual_cost": 0.0,
                "charge_outcome": "not_incurred", "status_code": 503,
                "attempt": 1, "idempotent": True,
            },
            modality="image",
        )
        self.assertEqual(result["status"], "EXECUTION_FAILED")
        self.assertTrue(result["retry_decision"]["retry"])
        self.assertEqual(result["retry_decision"]["reason"], "TRANSIENT_HTTP_FAILURE")

        unsafe = service.execute_routed_request(
            category="image", project_id="film", request_id="non-idempotent-provider-failure",
            estimated_cost=0.0,
            adapter=lambda **kwargs: {
                "started": True, "success": False, "actual_cost": 0.0,
                "charge_outcome": "not_incurred", "status_code": 503,
                "attempt": 1,
            },
            modality="image",
        )
        self.assertFalse(unsafe["retry_decision"]["retry"])
        self.assertEqual(unsafe["retry_decision"]["reason"], "NON_IDEMPOTENT_REQUEST")

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
        self.assertEqual(self.quota.data["requests"]["job-2"]["status"], "RELEASED")
        self.assertEqual(self.quota.usage(provider_id="image")["daily"], 0)

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

    def test_credit_exhaustion_error_blocks_retry_even_when_http_429(self):
        class RoutingStub:
            def route(self, **kwargs):
                return {
                    "allowed": True,
                    "provider": "image",
                    "billing_mode": "PAID_APPROVED",
                }

        class RetrySpy:
            def __init__(self):
                self.calls = 0

            def decide(self, **kwargs):
                self.calls += 1
                return type("Decision", (), {
                    "retry": True, "attempt": 1, "delay_seconds": 1.0,
                    "reason": "TRANSIENT_HTTP_FAILURE",
                })()

        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=2.0,
            request_id="credits-exhausted-job",
        )
        self.quota.reserve(provider_id="image", request_id="credits-exhausted-job")
        retry_spy = RetrySpy()
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            retry_policy=retry_spy,
            cost_policy=self.cost,
            quota_manager=self.quota,
        )
        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="credits-exhausted-job",
            estimated_cost=2.0,
            modality="image",
            adapter=lambda **kwargs: {
                "started": True,
                "success": False,
                "actual_cost": 0.0,
                "charge_outcome": "unknown",
                "status_code": 429,
                "provider_error_code": "insufficient_quota",
                "idempotent": True,
            },
        )
        self.assertEqual(result["provider_error"]["status"], "CREDITS_EXHAUSTED")
        self.assertFalse(result["provider_error"]["retryable"])
        self.assertFalse(result["retry_decision"]["retry"])
        self.assertEqual(result["retry_decision"]["reason"], "CREDITS_EXHAUSTED")
        self.assertEqual(retry_spy.calls, 0)
        self.assertNotIn("provider_code", result["provider_error"])
        self.assertTrue(self.cost.ledger["projects"]["film"]["reservations"]["credits-exhausted-job"])

    def test_execution_queues_budget_alert_after_spend_crosses_threshold(self):
        self.cost.reserve_usage(
            project_id="film", provider_id="image", estimated_cost=4.0,
            request_id="prior-budget-job",
        )
        prior = self.cost.finalize_usage(
            project_id="film", provider_id="image", estimated_cost=4.0,
            actual_cost=4.0, request_id="prior-budget-job", success=True,
            charge_outcome="incurred",
        )
        self.assertTrue(prior.get("saved"))
        outer = self

        class RoutingStub:
            def route(self, **kwargs):
                reserved = outer.cost.reserve_usage(
                    project_id=kwargs["project_id"],
                    provider_id="image",
                    estimated_cost=kwargs["estimated_cost"],
                    request_id=kwargs["request_id"],
                )
                quota = outer.quota.reserve(
                    provider_id="image", request_id=kwargs["request_id"]
                )
                if reserved.get("status") != "RESERVED" or quota.get("status") != "RESERVED":
                    return {"allowed": False, "status": "RESERVATION_FAILED"}
                return {
                    "allowed": True,
                    "provider": "image",
                    "billing_mode": "PAID_APPROVED",
                }

        alerts = BudgetAlertManager(self.root / "budget-alerts.json")
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            cost_policy=self.cost,
            quota_manager=self.quota,
            budget_alert_manager=alerts,
        )
        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="budget-alert-job",
            estimated_cost=2.0,
            modality="image",
            adapter=lambda **kwargs: {
                "started": True,
                "success": False,
                "actual_cost": 2.0,
                "charge_outcome": "incurred",
                "error_code": "PROVIDER_EXECUTION_FAILED",
            },
        )
        self.assertEqual(result["budget_alerts"]["status"], "BUDGET_ALERTS_QUEUED")
        self.assertEqual(
            [item["threshold"] for item in result["budget_alerts"]["alerts"]],
            [0.5],
        )
        self.assertEqual(len(alerts.pending_alerts()["alerts"]), 1)

    def test_least_privilege_authorization_blocks_generation_before_adapter(self):
        authorization = ProviderAuthorizationManager(self.root / "service-auth.json")
        authorization.assign_role("read-only-user", "viewer")
        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
        )
        called = []
        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="auth-denied-job",
            estimated_cost=0.0,
            modality="image",
            principal_id="read-only-user",
            adapter=lambda **kwargs: called.append(kwargs),
        )
        self.assertEqual(result["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(result["authorization"]["status"], "ACCESS_DENIED")
        self.assertFalse(result["executed"])
        self.assertEqual(called, [])
        missing_identity = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="auth-missing-principal-job",
            estimated_cost=0.0,
            modality="image",
            adapter=lambda **kwargs: called.append(kwargs),
        )
        self.assertEqual(missing_identity["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(missing_identity["authorization"]["status"], "INVALID_PRINCIPAL_ID")
        self.assertEqual(called, [])

    def test_sensitive_credentials_are_blocked_before_routing_and_adapter(self):
        calls = {"route": 0, "adapter": 0}

        class RoutingStub:
            def route(self, **kwargs):
                calls["route"] += 1
                return {"allowed": True, "provider": "image", "billing_mode": "ZERO_COST"}

        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            sensitive_data_policy=SensitiveDataPolicy(),
        )
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

        def adapter(**kwargs):
            calls["adapter"] += 1
            return {"started": False, "success": False}

        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="sensitive-data-job",
            estimated_cost=0.0,
            modality="image",
            adapter=adapter,
            adapter_options={"prompt": "generate with Authorization: Bearer " + secret},
        )
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["executed"])
        self.assertEqual(calls, {"route": 0, "adapter": 0})
        self.assertNotIn(secret, repr(result))
        self.assertNotIn("Bearer", repr(result))

    def test_unknown_provider_data_policy_blocks_adapter_execution(self):
        calls = []

        class RoutingStub:
            def route(self, **kwargs):
                return {
                    "allowed": True,
                    "provider": "unlisted-provider",
                    "billing_mode": "ZERO_COST",
                }

        policy = ProviderDataHandlingPolicy({
            "image": {
                "allowed_categories": ["public", "user_content"],
                "max_retention_days": 0,
                "training_use_allowed": False,
                "source_url": "https://provider.example.com/privacy",
            }
        })
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            data_handling_policy=policy,
        )
        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="data-policy-job",
            estimated_cost=0.0,
            modality="image",
            adapter=lambda **kwargs: calls.append(kwargs),
        )
        self.assertEqual(result["status"], "PROVIDER_DATA_POLICY_REJECTED")
        self.assertEqual(result["data_policy"]["status"], "PROVIDER_DATA_POLICY_UNKNOWN")
        self.assertFalse(result["executed"])
        self.assertEqual(calls, [])

    def test_provider_verification_requires_verify_permission(self):
        authorization = ProviderAuthorizationManager(self.root / "verify-auth.json")
        authorization.assign_role("creator-only", "creator")
        authorization.assign_role("operator-user", "operator")
        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
        )
        calls = []
        denied = service.verify_provider(
            "image",
            lambda provider, **kwargs: calls.append(provider["id"]) or {"ok": True},
            principal_id="creator-only",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(calls, [])
        allowed = service.verify_provider(
            "image",
            lambda provider, **kwargs: calls.append(provider["id"]) or {"ok": True},
            principal_id="operator-user",
        )
        self.assertTrue(allowed["verified"])
        self.assertEqual(calls, ["image"])

    def test_webhook_verification_is_delegated_without_echoing_secret(self):
        class WebhookStub:
            def verify(self, **kwargs):
                return {
                    "verified": True,
                    "status": "WEBHOOK_VERIFIED",
                    "provider": kwargs["provider_id"],
                    "event_id": kwargs["event_id"],
                }

        service = ProviderService(
            self.connections,
            webhook_verifier=WebhookStub(),
        )
        secret = "do-not-echo-webhook-secret"
        result = service.verify_webhook(
            provider_id="video",
            payload=b'{"event":"done"}',
            signature="sha256=" + "a" * 64,
            timestamp="1792152000",
            event_id="event-1",
            secret=secret,
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "WEBHOOK_VERIFIED")
        self.assertNotIn(secret, repr(result))

    def test_async_job_queue_facade_enforces_access_and_backpressure(self):
        jobs = ProviderJobManager(
            self.root / "service-jobs.json",
            max_queue_size=1,
            max_concurrent=1,
        )
        authorization = ProviderAuthorizationManager(self.root / "job-auth.json")
        authorization.assign_role("viewer-job-user", "viewer")
        authorization.assign_role("creator-job-user", "creator")
        service = ProviderService(
            self.connections,
            job_manager=jobs,
            authorization_manager=authorization,
        )
        denied = service.submit_job(
            job_id="job-denied", project_id="film", provider_id="image",
            payload_ref="payload:denied", principal_id="viewer-job-user",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        accepted = service.submit_job(
            job_id="job-1", project_id="film", provider_id="image",
            payload_ref="payload:job-1", principal_id="creator-job-user",
        )
        self.assertEqual(accepted["status"], "JOB_QUEUED")
        full = service.submit_job(
            job_id="job-2", project_id="film", provider_id="image",
            payload_ref="payload:job-2", principal_id="creator-job-user",
        )
        self.assertEqual(full["status"], "QUEUE_FULL")
        claimed = service.claim_next_job(
            worker_id="worker-1", principal_id="creator-job-user",
        )
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        completed = service.complete_job(
            job_id="job-1", worker_id="worker-1", success=True,
            result_ref="asset:job-1", principal_id="creator-job-user",
        )
        self.assertEqual(completed["job"]["status"], "SUCCEEDED")
        self.assertEqual(
            service.poll_job(job_id="job-1", principal_id="viewer-job-user")["job"]["status"],
            "SUCCEEDED",
        )

    def test_execution_records_latency_queue_wait_and_failure_metrics(self):
        outer = self

        class RoutingStub:
            def route(self, **kwargs):
                cost = outer.cost.reserve_usage(
                    project_id=kwargs["project_id"],
                    provider_id="image",
                    estimated_cost=kwargs["estimated_cost"],
                    request_id=kwargs["request_id"],
                )
                quota = outer.quota.reserve(
                    provider_id="image", request_id=kwargs["request_id"]
                )
                if cost.get("status") != "RESERVED" or quota.get("status") != "RESERVED":
                    return {"allowed": False, "status": "RESERVATION_FAILED"}
                return {"allowed": True, "provider": "image", "billing_mode": "PAID_APPROVED"}

        metrics = ProviderMetricsManager()
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            cost_policy=self.cost,
            quota_manager=self.quota,
            metrics_manager=metrics,
        )
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="metrics-job",
            estimated_cost=2.0, modality="image",
            adapter=lambda **kwargs: {
                "started": True, "success": False, "actual_cost": 1.0,
                "charge_outcome": "incurred", "status_code": 503,
                "latency_ms": 25.0, "queue_wait_ms": 12.0,
            },
        )
        self.assertEqual(result["metrics"]["status"], "METRIC_RECORDED")
        snapshot = metrics.snapshot(provider_id="image")["metrics"]
        self.assertEqual(snapshot["failure_count"], 1)
        self.assertEqual(snapshot["avg_latency_ms"], 25.0)
        self.assertEqual(snapshot["avg_queue_wait_ms"], 12.0)

    def test_low_output_quality_score_prevents_success_status(self):
        router = ProviderRoutingManager(
            self.connections, cost_policy=self.cost, quota_manager=self.quota
        )
        quality = ProviderOutputQualityPolicy(
            lambda path, **kwargs: 0.2,
            thresholds={"image:image": 0.8},
        )
        service = ProviderService(
            self.connections,
            routing_manager=router,
            health_manager=self.health,
            retry_policy=self.retry,
            output_validator=self.validator,
            cost_policy=self.cost,
            quota_manager=self.quota,
            quality_policy=quality,
        )
        output_path = self.root / "low-quality.png"
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-test-image")
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="low-quality-job",
            estimated_cost=0.0, output_path=str(output_path),
            adapter=lambda **kwargs: {
                "started": True, "success": True, "actual_cost": 0.0,
                "charge_outcome": "not_incurred", "output_path": str(output_path),
                "latency_ms": 5.0,
            },
            modality="image",
        )
        self.assertEqual(result["status"], "OUTPUT_QUALITY_FAILED")
        self.assertEqual(result["quality"]["status"], "QUALITY_BELOW_THRESHOLD")
        self.assertFalse(result["quality"]["allowed"])
        self.assertTrue(result["output"]["valid"])
        self.assertNotEqual(result["status"], "EXECUTION_SUCCEEDED")

    def test_execution_records_provider_model_benchmark_sample(self):
        outer = self

        class RoutingStub:
            def route(self, **kwargs):
                cost = outer.cost.reserve_usage(
                    project_id=kwargs["project_id"], provider_id="image",
                    estimated_cost=kwargs["estimated_cost"], request_id=kwargs["request_id"],
                )
                quota = outer.quota.reserve(provider_id="image", request_id=kwargs["request_id"])
                if cost.get("status") != "RESERVED" or quota.get("status") != "RESERVED":
                    return {"allowed": False, "status": "RESERVATION_FAILED"}
                return {
                    "allowed": True, "provider": "image",
                    "billing_mode": "PAID_APPROVED", "model_id": "sdxl-base",
                }

        benchmark = ProviderBenchmarkManager()
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            output_validator=self.validator,
            cost_policy=self.cost,
            quota_manager=self.quota,
            benchmark_manager=benchmark,
        )
        output_path = self.root / "benchmark-output.png"
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-test-image")
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="benchmark-job",
            estimated_cost=1.0, output_path=str(output_path),
            adapter=lambda **kwargs: {
                "started": True, "success": True, "actual_cost": 0.0,
                "charge_outcome": "not_incurred", "output_path": str(output_path),
                "latency_ms": 12.0, "model_id": "sdxl-base",
                "benchmark_run_id": "nightly-run-1",
            },
            modality="image",
        )
        self.assertEqual(result["benchmark"]["status"], "BENCHMARK_SAMPLE_RECORDED")
        summary = benchmark.compare()["results"][0]
        self.assertEqual(summary["model_id"], "sdxl-base")
        self.assertEqual(summary["sample_count"], 1)
        self.assertEqual(summary["success_rate"], 1.0)

    def test_credit_exhaustion_opens_critical_incident(self):
        outer = self

        class RoutingStub:
            def route(self, **kwargs):
                cost = outer.cost.reserve_usage(
                    project_id=kwargs["project_id"], provider_id="image",
                    estimated_cost=kwargs["estimated_cost"], request_id=kwargs["request_id"],
                )
                quota = outer.quota.reserve(provider_id="image", request_id=kwargs["request_id"])
                if cost.get("status") != "RESERVED" or quota.get("status") != "RESERVED":
                    return {"allowed": False, "status": "RESERVATION_FAILED"}
                return {"allowed": True, "provider": "image", "billing_mode": "PAID_APPROVED"}

        incidents = ProviderIncidentManager(self.root / "service-incidents.json")
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            cost_policy=self.cost,
            quota_manager=self.quota,
            incident_manager=incidents,
        )
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="credits-incident-job",
            estimated_cost=2.0, modality="image",
            adapter=lambda **kwargs: {
                "started": True, "success": False, "actual_cost": 0.0,
                "charge_outcome": "unknown", "status_code": 429,
                "provider_error_code": "insufficient_quota",
            },
        )
        self.assertEqual(result["provider_error"]["status"], "CREDITS_EXHAUSTED")
        self.assertEqual(result["incident"]["status"], "INCIDENT_OPENED")
        self.assertEqual(result["incident"]["incident"]["severity"], "critical")
        self.assertEqual(incidents.snapshot()["counts"]["OPEN"], 1)

    def test_disabled_generation_feature_flag_blocks_routing_and_adapter(self):
        calls = {"route": 0, "adapter": 0}

        class RoutingStub:
            def route(self, **kwargs):
                calls["route"] += 1
                return {"allowed": True, "provider": "image"}

        flags = ProviderFeatureFlagManager(self.root / "service-flags.json")
        flags.set_flag("provider_generation", enabled=False, rollout_percent=100)
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            feature_flag_manager=flags,
        )
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="feature-disabled-job",
            estimated_cost=0.0, modality="image",
            adapter=lambda **kwargs: calls.__setitem__("adapter", calls["adapter"] + 1),
        )
        self.assertEqual(result["status"], "FEATURE_DISABLED")
        self.assertFalse(result["executed"])
        self.assertEqual(calls, {"route": 0, "adapter": 0})

    def test_invalid_media_container_blocks_audio_video_success(self):
        outer = self

        class RoutingStub:
            def route(self, **kwargs):
                cost = outer.cost.reserve_usage(
                    project_id=kwargs["project_id"], provider_id="image",
                    estimated_cost=kwargs["estimated_cost"], request_id=kwargs["request_id"],
                )
                quota = outer.quota.reserve(provider_id="image", request_id=kwargs["request_id"])
                if cost.get("status") != "RESERVED" or quota.get("status") != "RESERVED":
                    return {"allowed": False, "status": "RESERVATION_FAILED"}
                return {"allowed": True, "provider": "image", "billing_mode": "PAID_APPROVED"}

        class OutputStub:
            def validate_file(self, path, *, modality):
                return {"valid": True, "status": "BASIC_OUTPUT_VALID"}

        class MediaStub:
            def validate(self, path, *, modality):
                return {"valid": False, "status": "MEDIA_CONTAINER_INVALID"}

        service = ProviderService(
            self.connections, routing_manager=RoutingStub(),
            output_validator=OutputStub(), media_integrity_validator=MediaStub(),
            cost_policy=self.cost, quota_manager=self.quota,
        )
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="bad-media-job",
            estimated_cost=1.0, modality="audio",
            adapter=lambda **kwargs: {
                "started": True, "success": True, "actual_cost": 0.0,
                "charge_outcome": "not_incurred", "output_path": "fake.wav",
                "latency_ms": 1.0,
            },
        )
        self.assertEqual(result["status"], "OUTPUT_VALIDATION_FAILED")
        self.assertEqual(result["media_integrity"]["status"], "MEDIA_CONTAINER_INVALID")
        self.assertFalse(result["output"]["valid"])

    def test_unavailable_optional_managers_fail_with_explicit_status(self):
        service = ProviderService(self.connections)
        self.assertEqual(service.route_request(category="image", project_id="film")["status"], "ROUTING_MANAGER_UNAVAILABLE")
        self.assertEqual(service.validate_output("missing.png", modality="image")["status"], "OUTPUT_VALIDATOR_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
