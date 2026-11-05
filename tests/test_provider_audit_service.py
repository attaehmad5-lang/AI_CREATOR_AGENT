"""Service-facade integration tests for routing, approvals and managed secrets."""
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLogManager
from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.provider_service import ProviderService


class ConnectionStub:
    def __init__(self):
        self.provider = {"id": "image", "secret_ref": "vault-ref-provider-image"}

    def verify_connectivity(self, *args, **kwargs):
        return {"verified": False, "status": "NOT_PROBED"}

    def get_provider(self, provider_id):
        return self.provider if provider_id == "image" else None


class RoutingStub:
    def __init__(self, *, billing_mode=None):
        self.calls = []
        self.billing_mode = billing_mode

    def route(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "allowed": True,
            "status": "ROUTED",
            "provider": "image",
            "category": kwargs.get("category", "image"),
            "strategy": kwargs.get("strategy", "PRIMARY_ONLY"),
            "billing_mode": self.billing_mode,
            "prior_decisions": [],
        }


class SecretLease:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SecretManagerStub:
    tenant_isolation_manager = None

    def __init__(self):
        self.lease = SecretLease()
        self.rotations = []

    def resolve_secret(self, secret_ref, **kwargs):
        if secret_ref != "vault-ref-provider-image":
            raise AssertionError("unexpected secret reference")
        return self.lease

    def rotate_secret(self, secret_ref, **kwargs):
        self.rotations.append((secret_ref, dict(kwargs)))
        return {
            "status": "SECRET_ROTATED",
            "rotated": True,
            "backend": "test-vault",
            "version_id": "version-2",
            "rotated_at": "2026-11-05T12:00:00+00:00",
        }


class ProviderAuditServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.authorization = ProviderAuthorizationManager(root / "auth.json")
        self.authorization.assign_role("admin-user", "admin")
        self.authorization.assign_role("billing-user", "billing_manager")
        self.authorization.assign_role("creator-user", "creator")
        self.audit = ProviderAuditLogManager(root / "audit.ndjson")
        self.connection = ConnectionStub()
        self.secret = SecretManagerStub()

    def service(self, routing=None):
        return ProviderService(
            self.connection,
            routing_manager=routing or RoutingStub(),
            authorization_manager=self.authorization,
            secret_manager=self.secret,
            audit_log_manager=self.audit,
        )

    def test_route_decisions_are_logged_before_and_after_routing(self):
        service = self.service(RoutingStub(billing_mode="FREE_CREDITS"))
        result = service.route_request(
            category="image", project_id="project-1",
            request_id="route-1", estimated_cost=0.25,
            _audit_actor_principal_id="creator-user",
        )
        self.assertTrue(result["allowed"])
        events = self.audit.read_events()["events"]
        self.assertEqual([event["event_type"] for event in events], [
            "ROUTING_DECISION", "ROUTING_DECISION"
        ])
        self.assertEqual(events[0]["outcome"], "REQUESTED")
        self.assertEqual(events[1]["outcome"], "ALLOWED")
        self.assertEqual(events[1]["provider_id"], "image")
        self.assertEqual(events[1]["actor_principal_id"], "creator-user")

    def test_paid_route_requires_billing_approval_and_logs_denial(self):
        routing = RoutingStub(billing_mode="PAID_APPROVED")
        service = self.service(routing)
        result = service.route_request(
            category="image", project_id="project-1",
            request_id="paid-1", estimated_cost=2.0,
            paid_approved_providers={"image"},
            _audit_actor_principal_id="creator-user",
        )
        self.assertEqual(result["status"], "BILLING_APPROVAL_AUTHORIZATION_REQUIRED")
        self.assertEqual(routing.calls, [])
        events = self.audit.read_events()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "BILLING_APPROVAL")
        self.assertEqual(events[0]["outcome"], "DENIED")

    def test_billing_manager_approval_is_audited_and_applied_to_paid_route(self):
        service = self.service(RoutingStub(billing_mode="PAID_APPROVED"))
        result = service.route_request(
            category="image", project_id="project-1",
            request_id="paid-2", estimated_cost=2.0,
            paid_approved_providers={"image"},
            _audit_actor_principal_id="billing-user",
        )
        self.assertTrue(result["allowed"])
        events = self.audit.read_events()["events"]
        self.assertEqual(events[-1]["event_type"], "BILLING_APPROVAL")
        self.assertEqual(events[-1]["outcome"], "ALLOWED")
        self.assertTrue(events[-1]["metadata"]["approval_granted"])

    def test_credential_rotation_records_intent_and_version_without_secret(self):
        service = self.service()
        result = service.rotate_provider_secret(
            "image", idempotency_key="rotation-1",
            confirmed=True, principal_id="admin-user",
        )
        self.assertTrue(result["rotated"])
        events = self.audit.read_events()["events"]
        self.assertEqual([event["event_type"] for event in events], [
            "CREDENTIAL_ROTATION", "CREDENTIAL_ROTATION"
        ])
        self.assertEqual(events[0]["outcome"], "REQUESTED")
        self.assertEqual(events[1]["outcome"], "SUCCEEDED")
        self.assertEqual(events[1]["metadata"]["credential_backend"], "test-vault")
        self.assertEqual(events[1]["metadata"]["credential_version_id"], "version-2")
        raw = self.audit.log_path.read_text(encoding="utf-8")
        self.assertNotIn("vault-ref-provider-image", raw)
        self.assertNotIn("rotation-1", raw)

    def test_managed_credential_and_execution_lifecycle_are_audited(self):
        service = self.service(RoutingStub(billing_mode="ZERO_COST"))
        seen = {}

        def adapter(**kwargs):
            seen["lease"] = kwargs.get("provider_credential_lease")
            return {
                "started": True,
                "success": False,
                "actual_cost": 0.0,
                "charge_outcome": "not_incurred",
                "credit_outcome": "unknown",
                "status_code": 503,
            }

        result = service.execute_routed_request(
            category="image",
            project_id="project-1",
            request_id="execution-1",
            estimated_cost=0.0,
            adapter=adapter,
            modality="image",
            principal_id="admin-user",
            use_managed_credential=True,
        )
        self.assertTrue(result["executed"])
        self.assertTrue(self.secret.lease.closed)
        self.assertIs(seen["lease"], self.secret.lease)
        events = self.audit.read_events()["events"]
        types = [event["event_type"] for event in events]
        self.assertEqual(types.count("ROUTING_DECISION"), 2)
        self.assertEqual(types.count("CREDENTIAL_ACCESS"), 2)
        self.assertEqual(types.count("ROUTED_EXECUTION"), 2)
        credential_events = [event for event in events if event["event_type"] == "CREDENTIAL_ACCESS"]
        self.assertEqual([event["outcome"] for event in credential_events], ["REQUESTED", "SUCCEEDED"])
        execution_events = [event for event in events if event["event_type"] == "ROUTED_EXECUTION"]
        self.assertEqual([event["outcome"] for event in execution_events], ["REQUESTED", "FAILED"])
        raw = self.audit.log_path.read_text(encoding="utf-8")
        self.assertNotIn("vault-ref-provider-image", raw)
        self.assertNotIn("provider_credential_lease", raw)

    def test_audit_read_and_integrity_verification_require_provider_read(self):
        service = self.service()
        self.audit.record_event(
            "ROUTING_DECISION", outcome="ALLOWED", status_code="ROUTE_SELECTED",
            tenant_id="tenant-1", project_id="project-1",
        )
        result = service.read_provider_audit_events(
            principal_id="admin-user", tenant_id="tenant-1", project_id="project-1"
        )
        self.assertEqual(result["status"], "AUDIT_EVENTS_READ")
        self.assertTrue(result["verified"])
        self.assertTrue(service.verify_provider_audit_log(principal_id="admin-user")["valid"])
        denied = service.read_provider_audit_events(
            principal_id="creator-user", tenant_id="tenant-1", project_id="project-1"
        )
        self.assertEqual(denied["status"], "AUDIT_LOG_MANAGER_UNAVAILABLE" if self.audit is None else "AUDIT_EVENTS_READ")


if __name__ == "__main__":
    unittest.main()
