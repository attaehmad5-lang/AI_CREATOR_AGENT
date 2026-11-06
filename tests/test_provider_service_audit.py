"""ProviderService audit integration tests for routing, billing approvals and secrets."""
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLog
from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.provider_service import ProviderService


class ConnectionStub:
    def verify_connectivity(self, *args, **kwargs):
        return {"status": "CONNECTED", "verified": True}

    def get_provider(self, provider_id):
        return {
            "id": provider_id,
            "secret_ref": "vault/providers/" + provider_id + "/credential",
        }


class RoutingStub:
    def __init__(self, result=None):
        self.result = result or {
            "allowed": True,
            "status": "ROUTED",
            "provider": "image",
            "strategy": "FREE_CREDITS_FIRST",
            "estimated_cost": 0.0,
            "billing_mode": "ZERO_COST",
            "prior_decisions": [],
        }
        self.calls = []

    def route(self, **kwargs):
        self.calls.append(dict(kwargs))
        return dict(self.result)


class SecretManagerStub:
    def __init__(self, result=None):
        self.result = result or {
            "status": "SECRET_ROTATED",
            "rotated": True,
            "backend": "vault",
            "version_id": "version-2",
            "rotated_at": "2026-11-06T00:00:00+00:00",
        }
        self.calls = []

    def resolve_secret(self, *args, **kwargs):
        raise AssertionError("rotation test must not resolve/read the secret value")

    def rotate_secret(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return dict(self.result)


class BrokenAuditManager:
    def record(self, **kwargs):
        return {"status": "AUDIT_STORAGE_LIMIT_REACHED", "saved": False}

    def verify(self):
        return {"status": "AUDIT_LOG_INTEGRITY_FAILED", "valid": False}

    def read_events(self, **kwargs):
        return {"status": "AUDIT_LOG_INTEGRITY_FAILED", "events": []}


class ProviderServiceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.authorization = ProviderAuthorizationManager(self.root / "authorization.json")
        self.audit = ProviderAuditLog(
            self.root / "provider-audit.jsonl",
            signing_key=b"provider-audit-signing-key-is-32-bytes-minimum",
        )
        self.connections = ConnectionStub()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, *, router=None, auth=None, secret_manager=None, audit=None):
        return ProviderService(
            self.connections,
            routing_manager=router,
            authorization_manager=auth if auth is not None else self.authorization,
            secret_manager=secret_manager,
            provider_audit_manager=audit if audit is not None else self.audit,
        )

    def test_paid_route_requires_billing_approval_and_records_actor_and_decision(self):
        self.authorization.assign_role("billing-user", "billing_manager")
        router = RoutingStub({
            "allowed": True, "status": "ROUTED", "provider": "image",
            "strategy": "LOWEST_COST", "estimated_cost": 1.25,
            "billing_mode": "PAID_APPROVED", "prior_decisions": [],
        })
        service = self.service(router=router)
        result = service.route_request(
            principal_id="billing-user",
            tenant_id="tenant-a",
            category="image", project_id="film", request_id="req-paid-1",
            estimated_cost=1.25, strategy="LOWEST_COST",
            paid_approved_providers={"image"},
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(len(router.calls), 1)
        verification = self.audit.verify()
        self.assertTrue(verification["valid"])
        events = self.audit.read_events(limit=100)["events"]
        self.assertEqual(
            [item["event_type"] for item in events],
            [
                "provider.authorization.decision",
                "provider.approval.decision",
                "provider.routing.decision",
            ],
        )
        approval = events[1]
        self.assertTrue(approval["approved"])
        self.assertEqual(approval["provider_id"], "image")
        self.assertNotIn("billing-user", self.audit_path_text())
        self.assertNotIn("tenant-a", self.audit_path_text())
        self.assertNotIn("req-paid-1", self.audit_path_text())

    def test_creator_cannot_self_approve_paid_provider_route(self):
        self.authorization.assign_role("creator-user", "creator")
        router = RoutingStub()
        service = self.service(router=router)
        result = service.route_request(
            principal_id="creator-user",
            category="image", project_id="film", request_id="req-denied-1",
            estimated_cost=4.0, paid_approved_providers={"image"},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(router.calls, [])
        events = self.audit.read_events(limit=100)["events"]
        self.assertEqual(events[0]["reason_code"], "ACCESS_DENIED")
        self.assertFalse(events[1]["approved"])
        self.assertEqual(events[1]["reason_code"], "BILLING_APPROVAL_AUTHORIZATION_DENIED")

    def test_paid_approval_required_from_router_is_audited_as_denied(self):
        self.authorization.assign_role("creator-user", "creator")
        router = RoutingStub({
            "allowed": False,
            "status": "NO_POLICY_COMPLIANT_PROVIDER",
            "strategy": "LOWEST_COST",
            "decisions": [{"provider": "image", "status": "PAID_APPROVAL_REQUIRED"}],
        })
        service = self.service(router=router)
        result = service.route_request(
            principal_id="creator-user",
            category="image", project_id="film", request_id="req-no-approval",
            estimated_cost=2.0,
        )
        self.assertFalse(result["allowed"])
        events = self.audit.read_events(limit=100)["events"]
        self.assertTrue(any(
            item["event_type"] == "provider.approval.decision"
            and item["approved"] is False
            and item["reason_code"] == "PAID_APPROVAL_REQUIRED"
            for item in events
        ))

    def test_credential_rotation_records_request_and_success_without_secret_values(self):
        self.authorization.assign_role("security-admin", "admin")
        secrets = SecretManagerStub()
        service = self.service(secret_manager=secrets)
        result = service.rotate_provider_secret(
            "image",
            idempotency_key="rotation-request-1",
            confirmed=True,
            principal_id="security-admin",
            tenant_id="tenant-a",
        )
        self.assertTrue(result["rotated"])
        self.assertEqual(len(secrets.calls), 1)
        events = self.audit.read_events(limit=100)["events"]
        rotations = [item for item in events if item["event_type"] == "provider.credential.rotation"]
        self.assertEqual([item["outcome"] for item in rotations], ["REQUESTED", "SUCCEEDED"])
        raw = self.audit_path_text()
        self.assertNotIn("vault/providers/image/credential", raw)
        self.assertNotIn("version-2", raw)
        self.assertNotIn("rotation-request-1", raw)

    def test_audit_write_failure_fails_closed_for_authorization_and_routing(self):
        self.authorization.assign_role("creator-user", "creator")
        broken = BrokenAuditManager()
        service = self.service(router=RoutingStub(), audit=broken)
        auth_result = service._authorize_operation("creator-user", "provider.generate")
        self.assertEqual(auth_result["status"], "AUTHORIZATION_AUDIT_UNAVAILABLE")

        route_service = self.service(router=RoutingStub(), audit=broken)
        route_result = route_service.route_request(
            principal_id="creator-user", category="image", project_id="film",
            request_id="route-audit-unavailable", estimated_cost=0.0,
        )
        self.assertFalse(route_result["allowed"])
        self.assertEqual(route_result["status"], "AUDIT_LOG_WRITE_FAILED")
        self.assertTrue(route_result["reservations_retained"])

    def test_execute_routed_request_propagates_one_trace_across_routing_adapter_and_audit(self):
        self.authorization.assign_role("trace-admin", "admin")
        router = RoutingStub()
        service = self.service(router=router)
        adapter_calls = []
        parent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"

        result = service.execute_routed_request(
            category="image",
            project_id="film",
            estimated_cost=0.0,
            adapter=lambda **kwargs: adapter_calls.append(dict(kwargs)) or {"started": False},
            modality="image",
            principal_id="trace-admin",
            tenant_id="tenant-a",
            traceparent=parent,
        )

        self.assertEqual(result["status"], "EXECUTION_NOT_STARTED")
        self.assertRegex(result["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(result["trace_id"], "a" * 32)
        self.assertNotEqual(result["span_id"], "b" * 16)
        self.assertTrue(result["traceparent"].startswith("00-" + "a" * 32 + "-"))
        self.assertEqual(len(router.calls), 1)
        route_call = router.calls[0]
        self.assertEqual(route_call["request_id"], result["request_id"])
        self.assertEqual(route_call["trace_id"], result["trace_id"])
        self.assertRegex(route_call["span_id"], r"^[0-9a-f]{16}$")
        self.assertEqual(route_call["traceparent"].split("-")[1], result["trace_id"])
        self.assertEqual(len(adapter_calls), 1)
        self.assertEqual(adapter_calls[0]["request_id"], result["request_id"])
        self.assertEqual(adapter_calls[0]["trace_id"], result["trace_id"])
        self.assertRegex(adapter_calls[0]["span_id"], r"^[0-9a-f]{16}$")
        self.assertNotEqual(adapter_calls[0]["span_id"], route_call["span_id"])
        events = self.audit.read_events(limit=100)["events"]
        self.assertTrue(events)
        trace_refs = {event["trace_ref"] for event in events}
        self.assertEqual(len(trace_refs), 1)
        self.assertRegex(next(iter(trace_refs)), r"^[0-9a-f]{64}$")
        self.assertNotEqual(next(iter(trace_refs)), result["trace_id"])

    def test_invalid_incoming_traceparent_blocks_provider_routing(self):
        router = RoutingStub()
        service = self.service(router=router)
        result = service.execute_routed_request(
            category="image",
            project_id="film",
            estimated_cost=0.0,
            adapter=lambda **kwargs: self.fail("invalid trace must block adapter call"),
            modality="image",
            request_id="invalid-trace-request",
            traceparent="00-" + "0" * 32 + "-" + "b" * 16 + "-00",
        )
        self.assertEqual(result["status"], "INVALID_TRACEPARENT")
        self.assertEqual(router.calls, [])
        self.assertEqual(self.audit.verify()["event_count"], 0)

    def test_audit_read_and_verify_are_admin_only(self):
        self.authorization.assign_role("creator-user", "creator")
        self.authorization.assign_role("security-admin", "admin")
        service = self.service()
        denied = service.read_provider_audit_events(principal_id="creator-user")
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        verified = service.verify_provider_audit_log(principal_id="security-admin")
        self.assertTrue(verified["valid"])
        events = service.read_provider_audit_events(
            principal_id="security-admin", limit=100,
        )
        self.assertEqual(events["status"], "AUDIT_EVENTS_AVAILABLE")

    def audit_path_text(self):
        path = self.root / "provider-audit.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""


if __name__ == "__main__":
    unittest.main()
