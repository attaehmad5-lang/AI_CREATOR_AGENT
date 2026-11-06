import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLog
from projects.provider_store.provider_service import ProviderService


class _Connections:
    def verify_connectivity(self, provider_id, probe, **kwargs):
        return {"verified": True}

    def configure_provider(self, provider_id, **kwargs):
        return {"status": "CONFIGURED", "saved": True}

    def get_provider(self, provider_id):
        return {"id": provider_id, "secret_ref": "vault://provider-secret-ref"}


class _Authorization:
    supports_provider_scopes = True

    def __init__(self, allowed=True):
        self.allowed = allowed

    def authorize(self, principal_id, operation, **kwargs):
        return {"allowed": self.allowed, "status": "ALLOWED" if self.allowed else "DENIED"}

    def assign_role(self, target, role, *, enabled=True):
        return {"status": "ROLE_ASSIGNED", "saved": True}

    def set_provider_scopes(self, target, scopes):
        return {"status": "SCOPES_UPDATED", "saved": True}

    def disable_principal(self, target):
        return {"status": "PRINCIPAL_DISABLED", "saved": True}


class _Router:
    def __init__(self, result=None):
        self.calls = 0
        self.result = result or {
            "allowed": True, "status": "ROUTED", "provider": "image",
            "billing_mode": "ZERO_COST",
        }

    def route(self, **kwargs):
        self.calls += 1
        return dict(self.result)


class _SecretManager:
    tenant_isolation_manager = None

    def resolve_secret(self, *args, **kwargs):
        raise AssertionError("rotation test must not resolve existing secret")

    def rotate_secret(self, secret_ref, **kwargs):
        return {"rotated": True, "status": "SECRET_ROTATED", "backend": "test-vault"}


class ProviderServiceAuditIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = ProviderAuditLog(
            self.root / "audit.json", integrity_key=b"integration-test-key-with-32-bytes-minimum",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_authorization_decisions_are_recorded_with_minimal_context(self):
        service = ProviderService(
            _Connections(), authorization_manager=_Authorization(allowed=False),
            audit_log=self.log,
        )
        result = service._authorize_operation(
            "operator-1", "provider.credentials.rotate", provider_id="image",
        )
        self.assertEqual(result["status"], "DENIED")
        events = self.log.read_events(limit=10)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "authorization_decision")
        self.assertEqual(events[0]["outcome"], "denied")
        self.assertEqual(events[0]["actor_id"], "operator-1")
        self.assertNotIn("credential", repr(events[0]["details"]).casefold())

    def test_authorization_fails_closed_when_audit_persistence_fails(self):
        class BrokenAudit:
            def append(self, *args, **kwargs):
                return {"saved": False, "status": "AUDIT_PERSISTENCE_FAILED"}

        service = ProviderService(
            _Connections(), authorization_manager=_Authorization(),
            audit_log=BrokenAudit(),
        )
        result = service._authorize_operation("admin-1", "provider.configure")
        self.assertEqual(result["status"], "AUTHORIZATION_AUDIT_FAILED")

    def test_audit_reads_require_authorization_and_valid_integrity(self):
        service = ProviderService(
            _Connections(), authorization_manager=_Authorization(allowed=False),
            audit_log=self.log,
        )
        denied = service.read_provider_audit_events(principal_id="operator-1")
        self.assertEqual(denied["status"], "AUTHORIZATION_DENIED")
        self.assertEqual(denied["events"], [])

        admin_service = ProviderService(
            _Connections(), authorization_manager=_Authorization(allowed=True),
            audit_log=self.log,
        )
        page = admin_service.read_provider_audit_events(principal_id="admin-1")
        self.assertEqual(page["status"], "AUDIT_EVENTS_READ")
        self.assertTrue(page["events"])
        self.assertTrue(self.log.verify()["valid"])

    def test_audit_read_fails_closed_after_journal_tampering(self):
        self.log.append("routing_decision", outcome="success")
        import json
        state = json.loads((self.root / "audit.json").read_text(encoding="utf-8"))
        state["events"][0]["outcome"] = "failure"
        (self.root / "audit.json").write_text(json.dumps(state), encoding="utf-8")
        service = ProviderService(
            _Connections(), authorization_manager=_Authorization(allowed=True),
            audit_log=self.log,
        )
        result = service.read_provider_audit_events(principal_id="admin-1")
        self.assertEqual(result["status"], "AUTHORIZATION_AUDIT_FAILED")
        self.assertEqual(result["events"], [])

    def test_routing_attempt_and_outcome_are_recorded(self):
        router = _Router()
        service = ProviderService(_Connections(), routing_manager=router, audit_log=self.log)
        result = service.route_request(
            request_id="request-1", tenant_id="tenant-a",
            principal_id="user-1", category="image", project_id="film",
            estimated_cost=0.0,
        )
        self.assertTrue(result["allowed"])
        events = self.log.read_events(limit=10)["events"]
        self.assertEqual([event["outcome"] for event in events], ["attempted", "allowed"])
        self.assertEqual(events[1]["provider_id"], "image")
        self.assertEqual(events[1]["request_id"], "request-1")
        self.assertEqual(events[1]["tenant_id"], "tenant-a")

    def test_paid_usage_approval_decision_is_recorded_with_optional_approval_id(self):
        router = _Router(result={
            "allowed": True, "status": "ROUTED", "provider": "image",
            "billing_mode": "PAID_APPROVED",
        })
        service = ProviderService(_Connections(), routing_manager=router, audit_log=self.log)
        result = service.route_request(
            request_id="paid-request-1", tenant_id="tenant-a",
            principal_id="admin-1", approval_id="approval-42",
            category="image", project_id="film", estimated_cost=1.25,
        )
        self.assertTrue(result["allowed"])
        events = self.log.read_events(limit=10)["events"]
        approval = next(event for event in events if event["event_type"] == "approval_decision")
        self.assertEqual(approval["outcome"], "allowed")
        self.assertEqual(approval["details"]["approval_status"], "APPROVED")
        self.assertEqual(approval["details"]["approval_id"], "approval-42")

    def test_routing_is_not_started_when_attempt_audit_cannot_be_written(self):
        class BrokenAudit:
            def append(self, *args, **kwargs):
                return {"saved": False, "status": "AUDIT_PERSISTENCE_FAILED"}

        router = _Router()
        service = ProviderService(_Connections(), routing_manager=router, audit_log=BrokenAudit())
        result = service.route_request(
            request_id="request-2", category="image", project_id="film", estimated_cost=0.0,
        )
        self.assertEqual(result["status"], "ROUTING_AUDIT_FAILED")
        self.assertEqual(router.calls, 0)

    def test_provider_configuration_and_role_changes_are_audited(self):
        auth = _Authorization()
        service = ProviderService(
            _Connections(), authorization_manager=auth, audit_log=self.log,
        )
        configured = service.configure_provider(
            "image", principal_id="admin-1", model="image-model",
        )
        role = service.assign_provider_role(
            "operator-1", "operator", actor_principal_id="admin-1",
        )
        self.assertTrue(configured["saved"])
        self.assertTrue(role["saved"])
        events = self.log.read_events(limit=20)["events"]
        event_types = [event["event_type"] for event in events]
        self.assertIn("authorization_decision", event_types)
        self.assertIn("provider_configuration", event_types)
        self.assertIn("role_assignment", event_types)

    def test_credential_rotation_audit_never_records_secret_reference_or_value(self):
        service = ProviderService(
            _Connections(), authorization_manager=_Authorization(),
            secret_manager=_SecretManager(), audit_log=self.log,
        )
        result = service.rotate_provider_secret(
            "image", idempotency_key="rotate-idempotency-1",
            confirmed=True, principal_id="admin-1",
        )
        self.assertTrue(result["rotated"])
        raw = (self.root / "audit.json").read_text(encoding="utf-8")
        self.assertNotIn("vault://provider-secret-ref", raw)
        self.assertNotIn("rotate-idempotency-1", raw)
        self.assertIn("credential_rotation", raw)
        self.assertTrue(self.log.verify()["valid"])


if __name__ == "__main__":
    unittest.main()
