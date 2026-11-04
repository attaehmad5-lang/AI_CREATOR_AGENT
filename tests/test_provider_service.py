import json
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
from projects.provider_store.provider_slo_manager import ProviderSLOManager
from projects.provider_store.provider_degraded_mode import ProviderDegradedModeManager
from projects.provider_store.provider_data_policy import ProviderDataHandlingPolicy
from projects.provider_store.sensitive_data_policy import SensitiveDataPolicy
from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.project_policy_manager import ProviderProjectPolicyManager
from projects.provider_store.budget_alert_manager import BudgetAlertManager
from projects.provider_store.cost_policy import ProviderCostPolicy
from projects.provider_store.credit_manager import ProviderCreditManager
from projects.provider_store.routing_manager import ProviderRoutingManager
from projects.provider_store.health_manager import ProviderHealthManager
from projects.provider_store.output_validator import ProviderOutputValidator
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.quota_manager import ProviderQuotaManager
from projects.provider_store.retry_policy import RetryPolicy
from projects.generation.providers.pollinations_provider import PollinationsProviderError


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

    def test_project_policy_simulation_requires_admin_and_never_persists_candidate(self):
        policy_path = self.root / "project-policies.json"
        policy_manager = ProviderProjectPolicyManager(policy_path)
        original = {"allowed_providers": ["image"], "automatic_fallback": True}
        self.assertTrue(policy_manager.set_policy("film", original)["saved"])
        before = policy_path.read_bytes()

        authorization = ProviderAuthorizationManager(self.root / "policy-auth.json")
        authorization.assign_role("admin-1", "admin")
        authorization.assign_role("operator-1", "operator")
        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
            project_policy_manager=policy_manager,
        )
        scenarios = [{
            "request_id": "image-request",
            "provider_id": "image",
            "capability": "image_generation",
            "estimated_cost": 0.0,
        }]
        admin_result = service.simulate_project_provider_policy(
            "film",
            {"allowed_providers": ["other"]},
            scenarios,
            principal_id="admin-1",
        )
        self.assertEqual(admin_result["status"], "POLICY_SIMULATION_COMPLETED")
        self.assertFalse(admin_result["persisted"])
        self.assertFalse(admin_result["results"][0]["proposed"]["allowed"])
        self.assertEqual(policy_path.read_bytes(), before)
        self.assertEqual(policy_manager.get_policy("film")["policy"], original)

        operator_result = service.simulate_project_provider_policy(
            "film", {"allowed_providers": ["image"]}, scenarios,
            principal_id="operator-1",
        )
        self.assertEqual(operator_result["status"], "AUTHORIZATION_DENIED")
        self.assertFalse(operator_result["persisted"])

    def test_authorization_manager_credentials_are_withheld_from_service_results(self):
        token = "Bearer " + ("u" * 40)

        class AuthorizationStub:
            def authorize(self, principal_id, operation, **kwargs):
                return {
                    "allowed": False,
                    "status": "AUTHORIZATION_DENIED",
                    "diagnostics": {"access_token": token},
                }

        service = ProviderService(
            self.connections, authorization_manager=AuthorizationStub()
        )
        result = service._authorize_operation("principal-1", "provider.read")

        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertNotIn(token, repr(result))

    def test_project_policy_simulation_fails_closed_without_authorization_manager(self):
        result = ProviderService(self.connections).simulate_project_provider_policy(
            "film", {}, [{
                "provider_id": "image", "capability": "image_generation",
            }],
            principal_id="admin-1",
        )
        self.assertEqual(result["status"], "AUTHORIZATION_MANAGER_UNAVAILABLE")
        self.assertFalse(result["persisted"])

    def test_verify_provider_updates_connection_and_health_together(self):
        result = self.service.verify_provider("image", lambda provider, timeout_seconds: True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["connection"]["status"], "CONNECTED")
        self.assertEqual(result["health"]["status"], "HEALTHY")
        self.assertEqual(self.connections.get_state("image")["status"], "CONNECTED")

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
                self.request_headers = []

            def open(self, request, timeout):
                self.calls.append((request, timeout))
                self.request_headers.append({
                    key.casefold(): value for key, value in request.header_items()
                })
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
            opener.request_headers[0].get("authorization"), "Bearer do-not-persist"
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
                self.request_headers = []
            def open(self, request, timeout):
                self.calls.append((request, timeout))
                self.request_headers.append({
                    key.casefold(): value for key, value in request.header_items()
                })
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
        self.assertEqual(opener.request_headers[0].get("authorization"), "Bearer oauth-access-secret")
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

    def test_output_validator_response_with_credentials_is_not_returned(self):
        secret = "Bearer " + ("o" * 40)

        class OutputValidator:
            def validate_file(self, path, *, modality):
                return {
                    "valid": True,
                    "status": "OUTPUT_VALID",
                    "metadata": {"authorization": secret},
                }

        service = ProviderService(
            self.connections, output_validator=OutputValidator()
        )
        result = service.validate_output("output.png", modality="image")

        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertNotIn(secret, repr(result))

    def test_provider_connection_response_with_credentials_is_not_returned(self):
        secret = "ghp_" + ("x" * 36)

        class ConnectionStub:
            def verify_connectivity(self, provider_id, probe, **kwargs):
                return {
                    "status": "CONNECTED",
                    "verified": True,
                    "debug": {"api_key": secret},
                }

        service = ProviderService(ConnectionStub())
        result = service.verify_provider("image", lambda *_args, **_kwargs: True)

        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertNotIn(secret, repr(result))

    def test_retry_policy_input_with_credentials_is_blocked_before_decision(self):
        token = "Bearer " + ("q" * 40)

        class RetryStub:
            def __init__(self):
                self.calls = 0

            def decide(self, **failure):
                self.calls += 1
                return type("Decision", (), {
                    "retry": True, "attempt": 1,
                    "delay_seconds": 1.0, "reason": "RETRY_ALLOWED",
                })()

        retry = RetryStub()
        service = ProviderService(self.connections, retry_policy=retry)
        result = service.retry_decision(error=token)

        self.assertFalse(result["retry"])
        self.assertEqual(result["reason"], "SENSITIVE_DATA_BLOCKED")
        self.assertEqual(retry.calls, 0)
        self.assertNotIn(token, repr(result))

    def test_retry_policy_response_credentials_are_withheld_without_losing_retry_decision(self):
        token = "Bearer " + ("s" * 40)

        class RetryStub:
            def decide(self, **failure):
                return type("Decision", (), {
                    "retry": True, "attempt": token,
                    "delay_seconds": token, "reason": token,
                })()

        service = ProviderService(
            self.connections, retry_policy=RetryStub()
        )
        result = service.retry_decision(error="HTTP_503")

        self.assertTrue(result["retry"])
        self.assertEqual(result["reason"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertEqual(result["attempt"], 0)
        self.assertEqual(result["delay_seconds"], 0.0)
        self.assertNotIn(token, repr(result))

    def test_retry_policy_non_string_reason_is_not_returned(self):
        secret = "Bearer " + ("x" * 40)

        class RetryStub:
            def decide(self, **failure):
                return type("Decision", (), {
                    "retry": True,
                    "attempt": 1,
                    "delay_seconds": 0.25,
                    "reason": {"api_key": secret},
                })()

        service = ProviderService(self.connections, retry_policy=RetryStub())
        result = service.retry_decision(error="HTTP_503")

        self.assertTrue(result["retry"])
        self.assertEqual(result["reason"], "INVALID_RETRY_RESPONSE")
        self.assertNotIn(secret, repr(result))


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

    def _modality_service(self, **kwargs):
        class RoutingStub:
            def route(self, **route_kwargs):
                return {"allowed": True, "provider": "image", "billing_mode": "FREE"}
        return ProviderService(self.connections, routing_manager=RoutingStub(), **kwargs)

    def test_modality_binding_blocks_mismatched_output_before_adapter(self):
        calls = []
        service = self._modality_service()
        result = service.execute_routed_request(category="image", project_id="film", request_id="modality-mismatch",
            estimated_cost=0.0, modality="video", adapter=lambda **kwargs: calls.append(kwargs), require_modality_binding=True)
        self.assertEqual(result["status"], "MODALITY_MISMATCH")
        self.assertFalse(result["executed"])
        self.assertEqual(calls, [])
        self.assertEqual(result["modality_binding"]["output_modalities"], ["image"])
        self.assertEqual(result["modality_binding"]["requested_modality"], "video")

    def test_modality_binding_allows_matching_output_and_normalizes_case(self):
        calls = []
        service = self._modality_service()
        result = service.execute_routed_request(category="image", project_id="film", request_id="modality-match",
            estimated_cost=0.0, modality=" IMAGE ", adapter=lambda **kwargs: calls.append(kwargs) or {"started": False, "success": False},
            require_modality_binding=True)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(result["status"], {"MODALITY_MISMATCH", "PROVIDER_MODALITY_REJECTED"})

    def test_modality_binding_is_opt_in_and_preserves_legacy_behavior(self):
        calls = []
        service = self._modality_service()
        result = service.execute_routed_request(category="image", project_id="film", request_id="modality-legacy",
            estimated_cost=0.0, modality="video", adapter=lambda **kwargs: calls.append(kwargs) or {"started": False, "success": False})
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(result["status"], "MODALITY_MISMATCH")

    def test_modality_binding_fails_closed_for_bad_policy_and_bad_modality(self):
        calls = []
        service = self._modality_service()
        for index, bad_policy in enumerate(("yes", 1, None)):
            with self.subTest(policy=bad_policy):
                result = service.execute_routed_request(category="image", project_id="film", request_id=f"modality-policy-{index}",
                    estimated_cost=0.0, modality="image", adapter=lambda **kwargs: calls.append(kwargs), require_modality_binding=bad_policy)
                self.assertEqual(result["status"], "INVALID_MODALITY_BINDING_POLICY")
        for index, bad_modality in enumerate((None, 7, "", "hologram")):
            with self.subTest(modality=bad_modality):
                result = service.execute_routed_request(category="image", project_id="film", request_id=f"modality-bad-{index}",
                    estimated_cost=0.0, modality=bad_modality, adapter=lambda **kwargs: calls.append(kwargs), require_modality_binding=True)
                self.assertEqual(result["status"], "MODALITY_MISMATCH")
        self.assertEqual(calls, [])

    def test_modality_binding_fails_closed_when_connection_manager_cannot_report(self):
        class RoutingStub:
            def route(self, **kwargs):
                return {"allowed": True, "provider": "image", "billing_mode": "FREE"}
        class NoBindingConnections:
            verify_connectivity = staticmethod(lambda *a, **k: {})
        class RaisingConnections(NoBindingConnections):
            @staticmethod
            def get_modality_binding(provider_id):
                raise RuntimeError("secret internal detail")
        class MalformedConnections(NoBindingConnections):
            @staticmethod
            def get_modality_binding(provider_id):
                return ["not", "a", "dict"]
        expectations = ((NoBindingConnections, "MODALITY_BINDING_UNAVAILABLE"),
            (RaisingConnections, "MODALITY_BINDING_CHECK_FAILED"), (MalformedConnections, "INVALID_MODALITY_BINDING_RESPONSE"))
        for index, (connections_class, expected_status) in enumerate(expectations):
            with self.subTest(connections=connections_class.__name__):
                calls = []
                service = ProviderService(connections_class(), routing_manager=RoutingStub())
                service.release_unstarted_request = lambda **kwargs: {"released": True}
                result = service.execute_routed_request(category="image", project_id="film", request_id=f"modality-cm-{index}",
                    estimated_cost=0.0, modality="image", adapter=lambda **kwargs: calls.append(kwargs), require_modality_binding=True)
                self.assertEqual(result["status"], "PROVIDER_MODALITY_REJECTED")
                self.assertEqual(result["modality_binding"]["status"], expected_status)
                self.assertNotIn("secret internal detail", repr(result))
                self.assertEqual(calls, [])

    def test_modality_binding_releases_reservations_on_rejection(self):
        service = ProviderService(self.connections, routing_manager=self._modality_service().routing_manager,
            cost_policy=self.cost, quota_manager=self.quota)
        released = []
        original_release = service.release_unstarted_request
        def tracking_release(**kwargs):
            released.append(kwargs["request_id"])
            return original_release(**kwargs)
        service.release_unstarted_request = tracking_release
        result = service.execute_routed_request(category="image", project_id="film", request_id="modality-release",
            estimated_cost=0.0, modality="audio", adapter=lambda **kwargs: self.fail("adapter must not run"), require_modality_binding=True)
        self.assertEqual(result["status"], "MODALITY_MISMATCH")
        self.assertEqual(released, ["modality-release"])

    def test_routed_generation_health_gate_fails_closed_without_manager(self):
        service = self._modality_service()
        calls = []
        result = service.execute_routed_request(category="image", project_id="film", request_id="health-gate-no-manager",
            estimated_cost=0.0, modality="image", adapter=lambda **kwargs: calls.append(kwargs), require_fresh_health_check=True)
        self.assertEqual(result["status"], "PROVIDER_HEALTH_REJECTED")
        self.assertEqual(result["health"]["status"], "HEALTH_CHECK_UNAVAILABLE")
        self.assertEqual(calls, [])

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

    def test_pollinations_billing_exception_is_classified_and_reservations_are_retained(self):
        class RoutingStub:
            def route(self, **kwargs):
                return {
                    "allowed": True,
                    "provider": "pollinations",
                    "billing_mode": "PAID_APPROVED",
                }

        class BillingFailureAdapter:
            def __call__(self, **kwargs):
                raise PollinationsProviderError(
                    "safe message",
                    status_code=402,
                    retryable=False,
                    provider_error_code="insufficient_balance",
                )

        self.cost.reserve_usage(
            project_id="film", provider_id="pollinations", estimated_cost=2.0,
            request_id="pollinations-billing-exception",
        )
        pollinations_quota = ProviderQuotaManager(
            self.root / "pollinations-quota.json",
            limits={"pollinations": {"daily": 10}},
        )
        pollinations_quota.reserve(
            provider_id="pollinations", request_id="pollinations-billing-exception"
        )
        incidents = ProviderIncidentManager(self.root / "pollinations-incidents.json")
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            retry_policy=self.retry,
            cost_policy=self.cost,
            quota_manager=pollinations_quota,
            incident_manager=incidents,
        )
        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="pollinations-billing-exception",
            estimated_cost=2.0,
            adapter=BillingFailureAdapter(),
            modality="image",
        )
        self.assertEqual(result["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertEqual(result["provider_error"]["status"], "CREDITS_EXHAUSTED")
        self.assertFalse(result["provider_error"]["retryable"])
        self.assertFalse(result["retry_decision"]["retry"])
        self.assertTrue(result["reservations_retained"])
        self.assertIn(
            "pollinations-billing-exception",
            self.cost.ledger["projects"]["film"]["reservations"],
        )
        self.assertEqual(
            pollinations_quota.data["requests"]["pollinations-billing-exception"]["status"],
            "RESERVED",
        )
        self.assertEqual(result["incident"]["status"], "INCIDENT_OPENED")
        self.assertEqual(result["incident"]["incident"]["severity"], "critical")
        self.assertNotIn("safe message", repr(result))

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


    def test_provider_role_management_requires_admin_permission(self):
        authorization = ProviderAuthorizationManager(self.root / "role-management-auth.json")
        authorization.assign_role("admin-user", "admin")
        authorization.assign_role("operator-user", "operator")
        service = ProviderService(self.connections, authorization_manager=authorization)

        denied_assign = service.assign_provider_role(
            "new-user", "creator", actor_principal_id="operator-user"
        )
        denied_disable = service.disable_provider_principal(
            "admin-user", actor_principal_id="operator-user"
        )
        self.assertEqual(denied_assign["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(denied_disable["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(authorization.authorize("new-user", "provider.read")["status"], "PRINCIPAL_NOT_FOUND")
        self.assertTrue(authorization.authorize("admin-user", "provider.access.manage")["allowed"])

        assigned = service.assign_provider_role(
            "new-user", "creator", actor_principal_id="admin-user"
        )
        self.assertTrue(assigned["saved"])
        self.assertTrue(authorization.authorize("new-user", "provider.generate")["allowed"])
        disabled = service.disable_provider_principal(
            "new-user", actor_principal_id="admin-user"
        )
        self.assertTrue(disabled["saved"])
        self.assertEqual(authorization.authorize("new-user", "provider.generate")["status"], "PRINCIPAL_DISABLED")

    def test_attributed_request_fails_closed_without_authorization_manager(self):
        service = ProviderService(self.connections)
        called = []
        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="missing-auth-manager",
            estimated_cost=0.0,
            modality="image",
            principal_id="caller-1",
            adapter=lambda **kwargs: called.append(kwargs),
        )
        self.assertEqual(result["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(result["authorization"]["status"], "AUTHORIZATION_MANAGER_UNAVAILABLE")
        self.assertEqual(called, [])


    def test_provider_management_endpoints_require_admin_and_provider_scope(self):
        authorization = ProviderAuthorizationManager(self.root / "provider-management-auth.json")
        authorization.assign_role("admin-user", "admin")
        authorization.assign_role("creator-user", "creator")
        service = ProviderService(
            self.connections, authorization_manager=authorization
        )
        denied_configure = service.configure_provider(
            "image", principal_id="creator-user", model="private-model"
        )
        denied_disable = service.set_provider_enabled(
            "image", False, principal_id="creator-user"
        )
        self.assertEqual(denied_configure["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(denied_disable["status"], "AUTHORIZATION_DENIED")
        self.assertTrue(self.connections.is_provider_enabled("image"))

        configured = service.configure_provider(
            "image", principal_id="admin-user", model="safe-model"
        )
        self.assertEqual(configured["status"], "CONFIGURED")
        disabled = service.set_provider_enabled(
            "image", False, principal_id="admin-user"
        )
        self.assertTrue(disabled["saved"])
        enabled = service.set_provider_enabled(
            "image", True, principal_id="admin-user"
        )
        self.assertTrue(enabled["saved"])


    def test_provider_management_endpoints_fail_closed_without_authorization_manager(self):
        service = ProviderService(self.connections)
        configured = service.configure_provider(
            "image", principal_id=None, model="unprotected-model"
        )
        enabled = service.set_provider_enabled("image", False, principal_id=None)
        self.assertEqual(configured["status"], "AUTHORIZATION_MANAGER_UNAVAILABLE")
        self.assertEqual(enabled["status"], "AUTHORIZATION_MANAGER_UNAVAILABLE")
        self.assertTrue(self.connections.is_provider_enabled("image"))


    def test_provider_scoped_actions_fail_closed_for_legacy_authorization_adapters(self):
        class LegacyAuthorization:
            def authorize(self, principal_id, operation):
                return {"allowed": True, "status": "AUTHORIZED"}

        legacy = ProviderService(
            self.connections, authorization_manager=LegacyAuthorization()
        )
        calls = []
        result = legacy.verify_provider(
            "image",
            lambda provider, **kwargs: calls.append(provider["id"]) or {"ok": True},
            principal_id="caller-1",
        )
        self.assertEqual(result["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(
            result["authorization"]["status"],
            "PROVIDER_SCOPE_ENFORCEMENT_UNAVAILABLE",
        )
        self.assertEqual(calls, [])

    def test_provider_scopes_gate_verification_and_generation_routing(self):
        authorization = ProviderAuthorizationManager(self.root / "provider-scopes-auth.json")
        authorization.assign_role("admin-user", "admin")
        authorization.assign_role("operator-user", "operator")
        authorization.assign_role("creator-user", "creator")
        service = ProviderService(
            self.connections,
            routing_manager=ProviderRoutingManager(
                self.connections, cost_policy=self.cost, quota_manager=self.quota
            ),
            cost_policy=self.cost,
            quota_manager=self.quota,
            authorization_manager=authorization,
        )
        scopes = service.set_provider_access_scopes(
            "operator-user",
            {"provider.verify": ["image"]},
            actor_principal_id="admin-user",
        )
        self.assertTrue(scopes["saved"])
        calls = []
        allowed = service.verify_provider(
            "image",
            lambda provider, **kwargs: calls.append(provider["id"]) or {"ok": True},
            principal_id="operator-user",
        )
        self.assertTrue(allowed["verified"])
        denied = service.verify_provider(
            "unlisted-provider",
            lambda provider, **kwargs: calls.append(provider["id"]) or {"ok": True},
            principal_id="operator-user",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(denied["authorization"]["status"], "PROVIDER_SCOPE_DENIED")
        self.assertEqual(calls, ["image"])

        assigned = service.set_provider_access_scopes(
            "creator-user",
            {"provider.generate": ["not-image"]},
            actor_principal_id="admin-user",
        )
        self.assertTrue(assigned["saved"])
        adapter_calls = []
        route_denied = service.execute_routed_request(
            category="image", project_id="film", request_id="provider-scope-denied",
            estimated_cost=0.0, modality="image", principal_id="creator-user",
            adapter=lambda **kwargs: adapter_calls.append(kwargs),
        )
        self.assertEqual(route_denied["status"], "ROUTING_REJECTED")
        self.assertEqual(adapter_calls, [])
        service.set_provider_access_scopes(
            "creator-user",
            {"provider.generate": ["image"]},
            actor_principal_id="admin-user",
        )
        route_allowed = service.execute_routed_request(
            category="image", project_id="film", request_id="provider-scope-allowed",
            estimated_cost=0.0, modality="image", principal_id="creator-user",
            adapter=lambda **kwargs: adapter_calls.append(kwargs) or {
                "started": True, "success": True, "actual_cost": 0.0,
                "charge_outcome": "not_incurred",
            },
        )
        self.assertTrue(route_allowed["executed"])
        self.assertEqual(len(adapter_calls), 1)

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

    def test_sensitive_data_policy_is_enabled_by_default_for_direct_routing(self):
        calls = []

        class RoutingStub:
            def route(self, **kwargs):
                calls.append(kwargs)
                return {"allowed": True, "provider": "image"}

        service = ProviderService(self.connections, routing_manager=RoutingStub())
        secret = "Bearer " + ("x" * 40)
        result = service.route_request(category="image", project_id="film", authorization=secret)

        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["allowed"])
        self.assertEqual(calls, [])
        self.assertNotIn(secret, repr(result))

    def test_sensitive_data_policy_is_enabled_by_default_before_adapter(self):
        calls = []
        service = ProviderService(self.connections, routing_manager=type(
            "RoutingStub", (), {"route": lambda self, **kwargs: {"allowed": True, "provider": "image", "billing_mode": "ZERO_COST"}}
        )())

        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="default-sensitive-policy",
            estimated_cost=0.0,
            modality="image",
            adapter=lambda **kwargs: calls.append(kwargs),
            adapter_options={"headers": {"Authorization": "Bearer " + ("z" * 40)}},
        )

        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["executed"])
        self.assertEqual(calls, [])

    def test_sensitive_data_policy_blocks_credentials_in_routing_response(self):
        secret = "sk-" + ("r" * 40)

        class RoutingStub:
            def route(self, **kwargs):
                return {
                    "allowed": True,
                    "provider": "image",
                    "diagnostics": {"api_key": secret},
                }

        service = ProviderService(self.connections, routing_manager=RoutingStub())
        result = service.route_request(category="image", project_id="film")

        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["allowed"])
        self.assertNotIn(secret, repr(result))

    def test_sensitive_data_policy_checks_entire_job_response_not_only_job_payload(self):
        secret = "ghp_" + ("a" * 40)
        result = ProviderService._sanitize_job_response(
            {
                "status": "JOB_COMPLETED",
                "job": {"job_id": "job-1", "status": "completed"},
                "diagnostics": {"access_token": secret},
            },
            SensitiveDataPolicy(),
        )

        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertIsNone(result["job"])
        self.assertNotIn(secret, repr(result))

    def test_webhook_verifier_response_is_scanned_before_return(self):
        secret = "Bearer " + ("w" * 40)

        class WebhookStub:
            def verify(self, **kwargs):
                return {
                    "verified": True,
                    "status": "WEBHOOK_VERIFIED",
                    "metadata": {"authorization": secret},
                }

        service = ProviderService(self.connections, webhook_verifier=WebhookStub())
        result = service.verify_webhook(
            provider_id="image",
            payload=b'{"event":"complete"}',
            signature="sha256=" + ("0" * 64),
            timestamp=1,
            event_id="event-1",
            secret="s" * 32,
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertNotIn(secret, repr(result))

    def test_execute_response_is_blocked_when_manager_returns_credential(self):
        secret = "hf_" + ("q" * 40)

        class RoutingStub:
            def route(self, **kwargs):
                return {
                    "allowed": True,
                    "provider": "image",
                    "billing_mode": "ZERO_COST",
                }

        class MetricsStub:
            def record(self, **kwargs):
                return {"status": "RECORDED", "metadata": {"access_token": secret}}

        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            metrics_manager=MetricsStub(),
        )
        service.finalize_started_request = lambda **kwargs: {"finalized": True}
        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="outbound-sensitive-response",
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

        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertTrue(result["executed"])
        self.assertTrue(result["accounting_finalized"])
        self.assertNotIn(secret, repr(result))


    def test_manual_provider_connection_response_is_scanned_before_return(self):
        secret = "Bearer " + ("m" * 40)

        class ConnectionStub:
            def verify_manual_connection(self, *args, **kwargs):
                return {
                    "status": "CONNECTED",
                    "verified": True,
                    "metadata": {"authorization": secret},
                }

        service = ProviderService(ConnectionStub())
        result = service.verify_manual_provider(
            "image", manual_session=object(), probe=lambda *_args, **_kwargs: True
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertNotIn(secret, repr(result))

    def test_slo_manager_response_is_scanned_before_return(self):
        secret = "sk-" + ("s" * 40)
        authorization = ProviderAuthorizationManager(self.root / "privacy-slo-auth.json")
        authorization.assign_role("privacy-admin", "admin")

        class SLOStub:
            def evaluate(self, slo_id):
                return {
                    "status": "SLO_EVALUATED",
                    "evaluation": {"api_key": secret},
                }

        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
            slo_manager=SLOStub(),
        )
        result = service.evaluate_service_level_objective(
            "privacy-slo", principal_id="privacy-admin"
        )

        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertIsNone(result["evaluation"])
        self.assertNotIn(secret, repr(result))


    def test_usage_finalization_response_with_credentials_is_not_returned(self):
        secret = "ghp_" + ("f" * 36)

        class CostPolicyStub:
            def finalize_usage(self, **kwargs):
                return {
                    "status": "FINALIZED",
                    "saved": True,
                    "metadata": {"api_key": secret},
                }

        service = ProviderService(self.connections, cost_policy=CostPolicyStub())
        result = service.finalize_started_request(
            project_id="film",
            provider_id="image",
            request_id="privacy-finalize-response",
            estimated_cost=0.0,
            actual_cost=0.0,
            success=True,
            charge_outcome="not_incurred",
        )

        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertTrue(result["finalized"])
        self.assertNotIn(secret, repr(result))

    def test_reservation_release_response_with_credentials_is_not_returned(self):
        secret = "Bearer " + ("r" * 40)

        class CostPolicyStub:
            def release_reservation(self, **kwargs):
                return {
                    "status": "RESERVATION_RELEASED",
                    "metadata": {"authorization": secret},
                }

        service = ProviderService(self.connections, cost_policy=CostPolicyStub())
        result = service.release_unstarted_request(
            project_id="film",
            provider_id="image",
            request_id="privacy-release-response",
        )

        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertTrue(result["released"])
        self.assertNotIn(secret, repr(result))


    def test_project_policy_simulation_blocks_sensitive_input_before_manager(self):
        calls = []
        authorization = ProviderAuthorizationManager(self.root / "privacy-policy-auth.json")
        authorization.assign_role("policy-admin", "admin")

        class PolicyManager:
            def simulate_policy(self, project_id, candidate_policy, scenarios):
                calls.append((project_id, candidate_policy, scenarios))
                return {
                    "status": "POLICY_SIMULATION_COMPLETED",
                    "project_id": project_id,
                    "persisted": False,
                }

        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
            project_policy_manager=PolicyManager(),
        )
        secret = "sk-" + ("p" * 40)
        result = service.simulate_project_provider_policy(
            "film",
            {"provider": "image", "api_key": secret},
            [{"provider": "image", "allowed": True}],
            principal_id="policy-admin",
        )

        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["persisted"])
        self.assertEqual(calls, [])
        self.assertNotIn(secret, repr(result))

    def test_provider_role_manager_response_is_scanned_before_return(self):
        secret = "Bearer " + ("a" * 40)
        authorization = ProviderAuthorizationManager(self.root / "privacy-role-auth.json")
        authorization.assign_role("role-admin", "admin")
        original_assign = authorization.assign_role

        def contaminated_assign(target_principal_id, role, *, enabled=True):
            result = original_assign(target_principal_id, role, enabled=enabled)
            return {**result, "metadata": {"authorization": secret}}

        authorization.assign_role = contaminated_assign
        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
        )
        result = service.assign_provider_role(
            "role-target",
            "creator",
            actor_principal_id="role-admin",
        )

        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertFalse(result["saved"])
        self.assertNotIn(secret, repr(result))


    def test_authorization_manager_response_with_credential_is_not_exposed(self):
        secret = "sk-" + ("z" * 40)

        class AuthorizationStub:
            def authorize(self, principal_id, operation):
                return {
                    "allowed": False,
                    "status": "AUTHORIZATION_DENIED",
                    "metadata": {"api_key": secret},
                }

        service = ProviderService(
            self.connections,
            authorization_manager=AuthorizationStub(),
        )
        result = service._authorize_operation(
            "privacy-user", "provider.read", return_decision=True
        )

        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_OUTPUT_BLOCKED")
        self.assertNotIn(secret, repr(result))


    def test_claim_response_keeps_worker_token_but_never_exposes_lease_hash(self):
        lease_token = "worker-lease-capability"
        lease_hash = "private-lease-verifier-hash"

        class JobManagerStub:
            def claim_next(self, *, worker_id):
                return {
                    "status": "CLAIMED",
                    "job": {
                        "job_id": "job-privacy-1",
                        "lease_token": lease_token,
                        "lease_token_hash": lease_hash,
                    },
                }

        service = ProviderService(
            self.connections,
            job_manager=JobManagerStub(),
        )
        result = service.claim_next_job(worker_id="worker-privacy-1")

        self.assertEqual(result["status"], "CLAIMED")
        self.assertEqual(result["job"]["lease_token"], lease_token)
        self.assertNotIn("lease_token_hash", result["job"])
        self.assertNotIn(lease_hash, repr(result))


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

    def test_sensitive_metadata_in_routing_options_is_blocked_before_route(self):
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

        def adapter(**kwargs):
            calls["adapter"] += 1
            return {"started": False, "success": False}

        result = service.execute_routed_request(
            category="image",
            project_id="film",
            request_id="sensitive-routing-options",
            estimated_cost=0.0,
            modality="image",
            adapter=adapter,
            provider_metadata={"clientSecret": "opaque-value-not-for-logs"},
        )
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["executed"])
        self.assertEqual(calls, {"route": 0, "adapter": 0})
        self.assertNotIn("opaque-value-not-for-logs", repr(result))


    def test_direct_route_request_blocks_sensitive_metadata_before_routing(self):
        calls = []

        class RoutingStub:
            def route(self, **kwargs):
                calls.append(kwargs)
                return {"allowed": True, "provider": "image"}

        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            sensitive_data_policy=SensitiveDataPolicy(),
        )
        secret = "Bearer " + ("x" * 40)
        result = service.route_request(
            category="image",
            project_id="film",
            authorization=secret,
        )
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["allowed"])
        self.assertEqual(calls, [])
        self.assertNotIn(secret, repr(result))
        self.assertIn("authorization", repr(result["sensitive_data_scan"]["findings"]))

    def test_direct_route_request_fails_closed_when_sensitive_scan_raises(self):
        calls = []

        class RoutingStub:
            def route(self, **kwargs):
                calls.append(kwargs)
                return {"allowed": True, "provider": "image"}

        class BrokenPolicy:
            def inspect(self, payload):
                raise RuntimeError("sensitive internal details")

        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            sensitive_data_policy=BrokenPolicy(),
        )
        result = service.route_request(category="image", project_id="film")
        self.assertEqual(result["status"], "SENSITIVE_DATA_SCAN_FAILED")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("sensitive internal details", repr(result))
        self.assertEqual(calls, [])


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

    def test_routing_manager_response_is_scanned_before_service_return(self):
        secret = "Bearer " + ("r" * 40)

        class RoutingStub:
            def route(self, **kwargs):
                return {
                    "allowed": True,
                    "provider": "image",
                    "debug": {"access_token": secret},
                }

        service = ProviderService(self.connections, routing_manager=RoutingStub())
        result = service.route_request(category="image", project_id="film")

        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertNotIn(secret, repr(result))

    def test_webhook_verifier_response_is_scanned_before_service_return(self):
        secret = "Bearer " + ("w" * 40)

        class WebhookStub:
            def verify(self, **kwargs):
                return {
                    "verified": True,
                    "status": "WEBHOOK_VERIFIED",
                    "metadata": {"access_token": secret},
                }

        service = ProviderService(self.connections, webhook_verifier=WebhookStub())
        result = service.verify_webhook(
            provider_id="video", payload=b'{"event":"done"}',
            signature="sha256=" + "a" * 64, timestamp="1792152000",
            event_id="event-sensitive", secret="webhook-test-secret-value",
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertNotIn(secret, repr(result))

    def test_adapter_response_with_sensitive_data_is_blocked_without_releasing_reservations(self):
        secret = "Bearer " + ("a" * 40)

        class RoutingStub:
            def route(self, **kwargs):
                return {
                    "allowed": True, "provider": "image",
                    "billing_mode": "ZERO_COST",
                }

        service = ProviderService(self.connections, routing_manager=RoutingStub())
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="sensitive-adapter-response",
            estimated_cost=0.0, modality="image",
            adapter=lambda **kwargs: {
                "started": True, "success": False,
                "actual_cost": 0.0, "charge_outcome": "not_incurred",
                "provider_metadata": {"access_token": secret},
            },
        )

        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertEqual(result["execution_outcome"], "UNKNOWN")
        self.assertTrue(result["reservations_retained"])
        self.assertNotIn(secret, repr(result))

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

    def test_inbound_webhook_payload_credentials_are_blocked_before_verifier(self):
        calls = []

        class WebhookStub:
            def verify(self, **kwargs):
                calls.append(kwargs)
                return {"verified": True, "status": "WEBHOOK_VERIFIED"}

        service = ProviderService(self.connections, webhook_verifier=WebhookStub())
        token = "Bearer " + ("w" * 40)
        result = service.verify_webhook(
            provider_id="video",
            payload=("{\"event\":\"done\",\"authorization\":\""+token+"\"}").encode("utf-8"),
            signature="sha256=" + "a" * 64,
            timestamp="1792152000",
            event_id="event-credential-payload",
            secret="webhook-test-secret-value",
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertEqual(calls, [])
        self.assertNotIn(token, repr(result))

    def test_oversized_webhook_payload_is_rejected_before_decode_or_verifier(self):
        calls = []

        class WebhookStub:
            max_payload_bytes = 8

            def verify(self, **kwargs):
                calls.append(kwargs)
                return {"verified": True, "status": "WEBHOOK_VERIFIED"}

        service = ProviderService(self.connections, webhook_verifier=WebhookStub())
        result = service.verify_webhook(
            provider_id="video",
            payload=b"x" * 9,
            signature="sha256=" + "c" * 64,
            timestamp="1792152000",
            event_id="oversized-event",
            secret="webhook-test-secret-value",
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "WEBHOOK_PAYLOAD_TOO_LARGE")
        self.assertEqual(calls, [])

    def test_inbound_webhook_payload_rejects_non_utf8_bytes_before_verifier(self):
        calls = []

        class WebhookStub:
            def verify(self, **kwargs):
                calls.append(kwargs)
                return {"verified": True, "status": "WEBHOOK_VERIFIED"}

        service = ProviderService(self.connections, webhook_verifier=WebhookStub())
        result = service.verify_webhook(
            provider_id="video", payload=b"\xff\xfe",
            signature="sha256=" + "b" * 64, timestamp="1792152000",
            event_id="event-binary-payload", secret="webhook-test-secret-value",
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "INVALID_WEBHOOK_PAYLOAD_ENCODING")
        self.assertEqual(calls, [])

    def test_job_lease_renewal_facade_requires_authorized_principal(self):
        jobs = ProviderJobManager(self.root / "lease-renewal-jobs.json")
        authorization = ProviderAuthorizationManager(self.root / "lease-renewal-auth.json")
        authorization.assign_role("viewer-job-user", "viewer")
        service = ProviderService(
            self.connections, job_manager=jobs,
            authorization_manager=authorization,
        )
        denied = service.renew_job_lease(
            job_id="job-1", worker_id="worker-1", lease_token="x" * 43,
            principal_id="viewer-job-user",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")


    def test_provider_configuration_blocks_credentialed_endpoint_before_manager_write(self):
        authorization = ProviderAuthorizationManager(self.root / "config-privacy-auth.json")
        authorization.assign_role("admin-user", "admin")
        calls = []

        def configure_provider(*args, **kwargs):
            calls.append((args, kwargs))
            return {"status": "CONFIGURED", "saved": True}

        self.connections.configure_provider = configure_provider
        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
            sensitive_data_policy=SensitiveDataPolicy(),
        )
        secret = "endpoint-password"
        result = service.configure_provider(
            "image",
            principal_id="admin-user",
            endpoint=f"https://user:{secret}@api.example.com/v1",
        )
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["saved"])
        self.assertNotIn(secret, repr(result))
        self.assertEqual(calls, [])

    def test_access_control_writes_respect_configured_email_blocking(self):
        authorization = ProviderAuthorizationManager(self.root / "access-privacy-auth.json")
        authorization.assign_role("admin-user", "admin")
        authorization.assign_role("existing-person@example.com", "creator")
        service = ProviderService(
            self.connections,
            authorization_manager=authorization,
            sensitive_data_policy=SensitiveDataPolicy(
                blocked_categories=("credential", "email"),
            ),
        )

        assigned = service.assign_provider_role(
            "new-person@example.com",
            "creator",
            actor_principal_id="admin-user",
        )
        self.assertEqual(assigned["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(assigned["saved"])
        self.assertNotIn("new-person@example.com", authorization.data["principals"])

        scopes = service.set_provider_access_scopes(
            "existing-person@example.com",
            {"provider.generate": ["image"]},
            actor_principal_id="admin-user",
        )
        self.assertEqual(scopes["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(scopes["saved"])
        self.assertNotIn("provider_scopes", authorization.data["principals"]["existing-person@example.com"])

        disabled = service.disable_provider_principal(
            "existing-person@example.com",
            actor_principal_id="admin-user",
        )
        self.assertEqual(disabled["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(disabled["saved"])
        self.assertTrue(authorization.data["principals"]["existing-person@example.com"]["enabled"])

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
        lease_token = claimed["job"]["lease_token"]
        self.assertTrue(lease_token)

        # A general read caller can inspect running-job status but must not
        # receive the worker capability that is needed to complete the job.
        running_status = service.poll_job(
            job_id="job-1", principal_id="viewer-job-user",
        )
        self.assertEqual(running_status["job"]["status"], "RUNNING")
        self.assertNotIn("lease_token", running_status["job"])
        self.assertNotIn(lease_token, repr(running_status))
        completed = service.complete_job(
            job_id="job-1", worker_id="worker-1",
            lease_token=claimed["job"]["lease_token"], success=True,
            result_ref="asset:job-1", principal_id="creator-job-user",
        )
        self.assertEqual(completed["job"]["status"], "SUCCEEDED")
        self.assertEqual(
            service.poll_job(job_id="job-1", principal_id="viewer-job-user")["job"]["status"],
            "SUCCEEDED",
        )


    def test_job_submission_blocks_credentialed_payload_reference_before_persisting(self):
        jobs = ProviderJobManager(self.root / "sensitive-submit-jobs.json")
        authorization = ProviderAuthorizationManager(self.root / "sensitive-submit-auth.json")
        authorization.assign_role("job-creator", "creator")
        service = ProviderService(
            self.connections,
            job_manager=jobs,
            authorization_manager=authorization,
            sensitive_data_policy=SensitiveDataPolicy(),
        )
        secret = "payload-reference-password"
        result = service.submit_job(
            job_id="sensitive-submit",
            project_id="film",
            provider_id="image",
            payload_ref=f"https://user:{secret}@storage.example/payload",
            principal_id="job-creator",
        )
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(result["accepted"])
        self.assertNotIn(secret, repr(result))
        self.assertEqual(jobs.poll(job_id="sensitive-submit")["status"], "JOB_NOT_FOUND")
        if jobs.state_path.exists():
            self.assertNotIn(secret, jobs.state_path.read_text(encoding="utf-8"))

    def test_job_claim_blocks_credential_shaped_worker_identity_before_persisting(self):
        jobs = ProviderJobManager(self.root / "sensitive-claim-jobs.json")
        authorization = ProviderAuthorizationManager(self.root / "sensitive-claim-auth.json")
        authorization.assign_role("job-creator", "creator")
        service = ProviderService(
            self.connections,
            job_manager=jobs,
            authorization_manager=authorization,
            sensitive_data_policy=SensitiveDataPolicy(),
        )
        accepted = service.submit_job(
            job_id="sensitive-claim",
            project_id="film",
            provider_id="image",
            payload_ref="payload:sensitive-claim",
            principal_id="job-creator",
        )
        self.assertTrue(accepted["accepted"])
        secret_worker_id = "Bearer " + ("x" * 40)
        rejected = service.claim_next_job(
            worker_id=secret_worker_id,
            principal_id="job-creator",
        )
        self.assertEqual(rejected["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertIsNone(rejected["job"])
        self.assertNotIn(secret_worker_id, repr(rejected))
        self.assertEqual(jobs.poll(job_id="sensitive-claim")["job"]["status"], "QUEUED")
        claimed = service.claim_next_job(
            worker_id="worker-safe",
            principal_id="job-creator",
        )
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        self.assertEqual(claimed["job"]["worker_id"], "worker-safe")

    def test_job_completion_blocks_credentialed_result_reference_before_persisting(self):
        jobs = ProviderJobManager(self.root / "sensitive-complete-jobs.json")
        authorization = ProviderAuthorizationManager(self.root / "sensitive-complete-auth.json")
        authorization.assign_role("job-creator", "creator")
        service = ProviderService(
            self.connections,
            job_manager=jobs,
            authorization_manager=authorization,
            sensitive_data_policy=SensitiveDataPolicy(),
        )
        accepted = service.submit_job(
            job_id="sensitive-complete",
            project_id="film",
            provider_id="image",
            payload_ref="payload:sensitive-complete",
            principal_id="job-creator",
        )
        self.assertTrue(accepted["accepted"])
        claimed = service.claim_next_job(
            worker_id="worker-safe",
            principal_id="job-creator",
        )
        lease_token = claimed["job"]["lease_token"]
        secret = "result-reference-password"
        rejected = service.complete_job(
            job_id="sensitive-complete",
            worker_id="worker-safe",
            lease_token=lease_token,
            success=True,
            result_ref=f"https://user:{secret}@storage.example/result",
            principal_id="job-creator",
        )
        self.assertEqual(rejected["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertFalse(rejected["saved"])
        self.assertNotIn(secret, repr(rejected))
        running = service.poll_job(
            job_id="sensitive-complete",
            principal_id="job-creator",
        )
        self.assertEqual(running["job"]["status"], "RUNNING")
        self.assertNotIn("lease_token", running["job"])
        completed = service.complete_job(
            job_id="sensitive-complete",
            worker_id="worker-safe",
            lease_token=lease_token,
            success=True,
            result_ref="asset:clean-result",
            principal_id="job-creator",
        )
        self.assertEqual(completed["status"], "JOB_COMPLETED")
        self.assertEqual(completed["job"]["result_ref"], "asset:clean-result")


    def test_job_response_sanitizer_removes_raw_and_hashed_lease_verifiers(self):
        secret = "A" * 43
        digest = "b" * 64
        result = ProviderService._sanitize_job_response({
            "status": "JOB_STATUS",
            "job": {
                "job_id": "job-privacy",
                "lease_token": secret,
                "lease_token_hash": digest,
            },
        })
        self.assertNotIn("lease_token", result["job"])
        self.assertNotIn("lease_token_hash", result["job"])
        self.assertNotIn(secret, repr(result))
        self.assertNotIn(digest, repr(result))


    def test_cancel_response_redacts_running_worker_lease_token(self):
        jobs = ProviderJobManager(self.root / "cancel-redaction-jobs.json")
        authorization = ProviderAuthorizationManager(self.root / "cancel-redaction-auth.json")
        authorization.assign_role("viewer", "viewer")
        authorization.assign_role("creator", "creator")
        service = ProviderService(
            self.connections, job_manager=jobs,
            authorization_manager=authorization,
        )
        submitted = service.submit_job(
            job_id="cancel-redaction-job", project_id="film", provider_id="image",
            payload_ref="payload:cancel-redaction", principal_id="creator",
        )
        self.assertTrue(submitted["accepted"])
        claimed = service.claim_next_job(worker_id="worker-redaction", principal_id="creator")
        lease_token = claimed["job"]["lease_token"]
        cancelled = service.cancel_job(
            job_id="cancel-redaction-job", principal_id="creator",
        )
        self.assertEqual(cancelled["status"], "CANCEL_REQUESTED")
        self.assertNotIn("lease_token", cancelled["job"])
        self.assertNotIn(lease_token, repr(cancelled))



    def test_sensitive_stored_job_record_is_blocked_before_read_response(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

        class SensitiveJobManager:
            def submit(self, **kwargs):
                return {"status": "JOB_QUEUED", "accepted": True}

            def poll(self, *, job_id):
                return {
                    "status": "JOB_STATUS",
                    "job": {
                        "job_id": job_id,
                        "provider_id": "image",
                        "project_id": "film",
                        "payload_ref": "payload:safe-reference",
                        "notes": "Authorization: Bearer " + secret,
                    },
                }

        authorization = ProviderAuthorizationManager(self.root / "job-read-privacy-auth.json")
        authorization.assign_role("privacy-viewer", "viewer")
        service = ProviderService(
            self.connections,
            job_manager=SensitiveJobManager(),
            authorization_manager=authorization,
            sensitive_data_policy=SensitiveDataPolicy(),
        )
        result = service.poll_job(
            job_id="sensitive-stored-job", principal_id="privacy-viewer",
        )
        self.assertEqual(result["status"], "SENSITIVE_DATA_BLOCKED")
        self.assertIsNone(result["job"])
        self.assertNotIn(secret, repr(result))
        self.assertNotIn("Bearer", repr(result))
        self.assertEqual(
            result["sensitive_data_scan"]["blocked_categories"],
            ["credential"],
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
        slo = ProviderSLOManager(self.root / "execution-slos.json")
        slo.configure(
            "image-slo", provider_id="image", category="image",
            availability_target=0.5, max_p95_latency_ms=100,
            window_seconds=3600, minimum_samples=1,
        )
        service = ProviderService(
            self.connections,
            routing_manager=RoutingStub(),
            cost_policy=self.cost,
            quota_manager=self.quota,
            metrics_manager=metrics,
            slo_manager=slo,
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
        self.assertEqual(result["slo_observation"]["status"], "SLO_OBSERVATION_RECORDED")
        self.assertEqual(slo.evaluate("image-slo")["status"], "SLO_BREACHED")
        snapshot = metrics.snapshot(provider_id="image")["metrics"]
        self.assertEqual(snapshot["failure_count"], 1)
        self.assertEqual(snapshot["avg_latency_ms"], 25.0)
        self.assertEqual(snapshot["avg_queue_wait_ms"], 12.0)

    def test_service_uses_health_evidence_for_degraded_mode_and_blocks_category_outage(self):
        auth = ProviderAuthorizationManager(self.root / "degraded-auth.json")
        auth.assign_role("degraded-admin", "admin")
        auth.assign_role("degraded-operator", "operator")
        degraded = ProviderDegradedModeManager(self.root / "degraded-mode.json")
        service = ProviderService(
            self.connections,
            health_manager=self.health,
            authorization_manager=auth,
            degraded_mode_manager=degraded,
        )
        configured = service.configure_category_degraded_mode(
            "video", normal_min_healthy_providers=1,
            degraded_min_healthy_providers=1,
            recovery_success_samples=2,
            principal_id="degraded-admin",
        )
        self.assertEqual(configured["status"], "DEGRADED_MODE_POLICY_SAVED")
        denied = service.configure_category_degraded_mode(
            "image", principal_id="degraded-operator",
        )
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")

        called = []
        unknown_state = service.execute_routed_request(
            category="video", project_id="film", request_id="video-health-unknown",
            estimated_cost=0.0, modality="video",
            adapter=lambda **kwargs: called.append(True),
        )
        self.assertEqual(unknown_state["status"], "PROVIDER_CATEGORY_HEALTH_UNKNOWN")
        self.assertFalse(unknown_state["executed"])
        self.assertEqual(called, [])

        outage = service.evaluate_category_degraded_mode(
            "video", ["video-provider"], principal_id="degraded-operator",
        )
        self.assertEqual(outage["status"], "PROVIDER_CATEGORY_OUTAGE")
        self.assertEqual(outage["state"]["healthy_count"], 0)

        blocked = service.execute_routed_request(
            category="video", project_id="film", request_id="video-outage",
            estimated_cost=0.0, modality="video",
            adapter=lambda **kwargs: called.append(True),
        )
        self.assertEqual(blocked["status"], "PROVIDER_CATEGORY_OUTAGE")
        self.assertFalse(blocked["executed"])
        self.assertEqual(called, [])

    def test_degraded_mode_evaluation_fails_closed_without_health_or_authorization(self):
        degraded = ProviderDegradedModeManager(self.root / "degraded-no-auth.json")
        degraded.configure("image")
        no_auth = ProviderService(
            self.connections,
            health_manager=self.health,
            degraded_mode_manager=degraded,
        )
        self.assertEqual(
            no_auth.evaluate_category_degraded_mode("image", ["image"])["status"],
            "AUTHORIZATION_MANAGER_UNAVAILABLE",
        )
        auth = ProviderAuthorizationManager(self.root / "degraded-no-health-auth.json")
        auth.assign_role("admin", "admin")
        no_health = ProviderService(
            self.connections,
            authorization_manager=auth,
            degraded_mode_manager=degraded,
        )
        self.assertEqual(
            no_health.evaluate_category_degraded_mode(
                "image", ["image"], principal_id="admin",
            )["status"],
            "HEALTH_MANAGER_UNAVAILABLE",
        )

    def test_service_configures_slos_for_admin_and_limits_read_to_authorized_principals(self):
        auth = ProviderAuthorizationManager(self.root / "slo-auth.json")
        auth.assign_role("slo-admin", "admin")
        auth.assign_role("slo-operator", "operator")
        auth.assign_role("slo-viewer", "viewer")
        slo = ProviderSLOManager(self.root / "slo-state.json")
        service = ProviderService(
            self.connections, authorization_manager=auth, slo_manager=slo,
        )
        configured = service.configure_service_level_objective(
            "image-slo", provider_id="image", category="image",
            availability_target=0.99, max_p95_latency_ms=500,
            window_seconds=3600, minimum_samples=2,
            principal_id="slo-admin",
        )
        self.assertEqual(configured["status"], "SLO_CONFIGURED")
        denied_config = service.configure_service_level_objective(
            "operator-slo", provider_id="image", category="image",
            availability_target=0.99, max_p95_latency_ms=500,
            principal_id="slo-operator",
        )
        self.assertEqual(denied_config["status"], "AUTHORIZATION_DENIED")
        denied_read = service.evaluate_service_level_objective(
            "image-slo", principal_id="unknown",
        )
        self.assertEqual(denied_read["status"], "AUTHORIZATION_DENIED")
        allowed_read = service.evaluate_service_level_objective(
            "image-slo", principal_id="slo-viewer",
        )
        self.assertEqual(allowed_read["status"], "INSUFFICIENT_DATA")
        self.assertEqual(allowed_read["evaluation"]["sample_count"], 0)

    def test_service_slo_configuration_and_evaluation_fail_closed_without_authorization(self):
        service = ProviderService(
            self.connections,
            slo_manager=ProviderSLOManager(self.root / "slo-no-auth.json"),
        )
        configured = service.configure_service_level_objective(
            "image-slo", provider_id="image", category="image",
            availability_target=0.99, max_p95_latency_ms=500,
        )
        self.assertEqual(configured["status"], "AUTHORIZATION_MANAGER_UNAVAILABLE")
        evaluated = service.evaluate_service_level_objective("image-slo")
        self.assertEqual(evaluated["status"], "AUTHORIZATION_MANAGER_UNAVAILABLE")

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

    def test_inconsistent_degraded_mode_state_fails_closed_before_adapter(self):
        import copy

        valid_state = {
            "category": "image",
            "mode": "NORMAL",
            "healthy_provider_ids": ["image"],
            "unhealthy_provider_ids": ["fallback"],
            "healthy_count": 1,
            "provider_count": 2,
        }
        invalid_cases = []
        wrong_category = copy.deepcopy(valid_state)
        wrong_category["category"] = "video"
        invalid_cases.append(("wrong category", wrong_category, "PROVIDER_CATEGORY_NORMAL"))
        duplicate_ids = copy.deepcopy(valid_state)
        duplicate_ids["healthy_provider_ids"] = ["image", "image"]
        duplicate_ids["healthy_count"] = 2
        duplicate_ids["provider_count"] = 3
        invalid_cases.append(("duplicate provider IDs", duplicate_ids, "PROVIDER_CATEGORY_NORMAL"))
        overlapping_ids = copy.deepcopy(valid_state)
        overlapping_ids["unhealthy_provider_ids"] = ["image"]
        invalid_cases.append(("overlapping provider IDs", overlapping_ids, "PROVIDER_CATEGORY_NORMAL"))
        inconsistent_count = copy.deepcopy(valid_state)
        inconsistent_count["provider_count"] = 99
        invalid_cases.append(("inconsistent provider count", inconsistent_count, "PROVIDER_CATEGORY_NORMAL"))
        invalid_outage = copy.deepcopy(valid_state)
        invalid_outage["mode"] = "OUTAGE"
        invalid_cases.append(("outage with healthy provider", invalid_outage, "PROVIDER_CATEGORY_OUTAGE"))
        invalid_status = copy.deepcopy(valid_state)
        invalid_cases.append(("mismatched result status", invalid_status, "PROVIDER_CATEGORY_DEGRADED"))

        for label, state, response_status in invalid_cases:
            with self.subTest(case=label):
                class InjectedDegradedMode:
                    def evaluate(self, category, provider_statuses):
                        return {"status": "UNUSED", "evaluated": False}

                    def get_state(self, category):
                        return {"status": response_status, "state": state}

                called = []
                service = ProviderService(
                    self.connections, degraded_mode_manager=InjectedDegradedMode()
                )
                result = service.execute_routed_request(
                    category="image", project_id="film",
                    request_id=f"invalid-degraded-{label.replace(' ', '-')}",
                    estimated_cost=0.0, modality="image",
                    adapter=lambda **kwargs: called.append(True),
                )
                self.assertEqual(result["status"], "PROVIDER_CATEGORY_HEALTH_UNKNOWN")
                self.assertFalse(result["executed"])
                self.assertEqual(
                    result["degraded_mode"]["status"], "INVALID_DEGRADED_MODE_STATE"
                )
                self.assertEqual(called, [])

    def test_unknown_degraded_mode_value_fails_closed_before_adapter(self):
        class CorruptDegradedMode:
            def evaluate(self, category, provider_statuses):
                return {"status": "UNKNOWN", "evaluated": False}

            def get_state(self, category):
                return {"status": "UNKNOWN", "state": {
                    "category": category, "mode": ["CORRUPT"],
                    "healthy_provider_ids": ["image"],
                }}

        called = []
        service = ProviderService(self.connections, degraded_mode_manager=CorruptDegradedMode())
        result = service.execute_routed_request(
            category="image", project_id="film", request_id="corrupt-mode-request",
            estimated_cost=0.0, modality="image",
            adapter=lambda **kwargs: called.append(True),
        )
        self.assertEqual(result["status"], "PROVIDER_CATEGORY_HEALTH_UNKNOWN")
        self.assertFalse(result["executed"])
        self.assertEqual(result["degraded_mode"]["status"], "INVALID_DEGRADED_MODE_STATE")
        self.assertEqual(called, [])

    def test_unavailable_optional_managers_fail_with_explicit_status(self):
        service = ProviderService(self.connections)
        self.assertEqual(service.route_request(category="image", project_id="film")["status"], "ROUTING_MANAGER_UNAVAILABLE")
        self.assertEqual(service.validate_output("missing.png", modality="image")["status"], "OUTPUT_VALIDATOR_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
