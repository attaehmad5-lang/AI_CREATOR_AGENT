"""Provider audit log chain integrity, privacy and concurrency tests."""
import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLog


def _append_audit_event(args):
    path, key, index = args
    manager = ProviderAuditLog(path, signing_key=key)
    return manager.record(
        event_type="provider.routing.decision",
        actor_id=f"operator-{index}",
        tenant_id="tenant-a",
        project_id="film",
        provider_id="image",
        request_id=f"request-{index}",
        operation="provider.route",
        outcome="ALLOWED",
        reason_code="ROUTED",
        strategy="FREE_CREDITS_FIRST",
        estimated_cost=0.0,
        billing_mode="ZERO_COST",
    )["saved"]


class ProviderAuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "provider-audit.jsonl"
        self.key = b"audit-test-signing-key-material-32-bytes-minimum"

    def tearDown(self):
        self.temp.cleanup()

    def manager(self, **kwargs):
        return ProviderAuditLog(self.path, signing_key=self.key, **kwargs)

    def record_route(self, manager, index=1):
        return manager.record(
            event_type="provider.routing.decision",
            actor_id="admin-user-1",
            tenant_id="tenant-a",
            project_id="documentary-film",
            provider_id="image",
            request_id=f"request-{index}",
            operation="provider.route",
            outcome="ALLOWED",
            reason_code="ROUTED",
            strategy="FREE_CREDITS_FIRST",
            estimated_cost=0.25,
            billing_mode="FREE_CREDITS",
        )

    def test_append_verify_and_read_metadata_events(self):
        manager = self.manager()
        result = self.record_route(manager)
        self.assertTrue(result["saved"])
        self.assertEqual(result["sequence"], 1)
        verification = manager.verify()
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["event_count"], 1)
        page = manager.read_events()
        self.assertEqual(page["status"], "AUDIT_EVENTS_AVAILABLE")
        self.assertEqual(page["total_events"], 1)
        self.assertEqual(page["events"][0]["provider_id"], "image")
        self.assertEqual(page["events"][0]["reason_code"], "ROUTED")

    def test_identity_and_request_ids_are_pseudonymized_not_persisted(self):
        manager = self.manager()
        self.record_route(manager)
        raw = self.path.read_text(encoding="utf-8")
        for private_reference in (
            "admin-user-1", "tenant-a", "documentary-film", "request-1",
        ):
            self.assertNotIn(private_reference, raw)
        event = manager.read_events()["events"][0]
        self.assertRegex(event["actor_ref"], r"^[0-9a-f]{64}$")
        self.assertRegex(event["tenant_ref"], r"^[0-9a-f]{64}$")
        self.assertNotIn("secret", raw.casefold())

    def test_trace_reference_is_hmac_pseudonymized_in_audit_record(self):
        trace_id = "a" * 32
        result = self.manager().record(
            event_type="provider.routing.decision",
            provider_id="image",
            request_id="trace-correlated-request",
            trace_id=trace_id,
            operation="provider.route",
            outcome="ALLOWED",
            reason_code="ROUTED",
        )
        self.assertTrue(result["saved"])
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(trace_id, raw)
        event = self.manager().read_events()["events"][0]
        self.assertRegex(event["trace_ref"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(event["trace_ref"], trace_id)

    def test_invalid_trace_reference_is_rejected(self):
        result = self.manager().record(
            event_type="provider.routing.decision",
            provider_id="image",
            trace_id="0" * 32,
            operation="provider.route",
            outcome="ALLOWED",
            reason_code="ROUTED",
        )
        self.assertFalse(result["saved"])
        self.assertEqual(self.manager().verify()["event_count"], 0)

    def test_credential_rotation_event_stores_backend_but_not_secret_reference(self):
        manager = self.manager()
        result = manager.record(
            event_type="provider.credential.rotation",
            actor_id="vault-admin",
            provider_id="pollinations",
            operation="provider.credentials.rotate",
            outcome="SUCCEEDED",
            reason_code="SECRET_ROTATED",
            credential_backend="vault",
            credential_version_id="version-12",
        )
        self.assertTrue(result["saved"])
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("secret_ref", raw)
        self.assertNotIn("version-12", raw)
        event = manager.read_events()["events"][0]
        self.assertEqual(event["credential_backend"], "vault")
        self.assertNotEqual(event["credential_version_ref"], "version-12")

    def test_audit_inputs_reject_unknown_fields_and_sensitive_metadata(self):
        manager = self.manager()
        raw_payload = self.record_route(manager, 2)
        self.assertFalse(manager.record(
            event_type="provider.routing.decision",
            outcome="ALLOWED", reason_code="ROUTED",
            payload={"authorization": "Bearer do-not-store"},
        )["saved"])
        self.assertFalse(manager.record(
            event_type="provider.credential.rotation",
            outcome="SUCCEEDED", reason_code="SECRET_ROTATED",
            credential_backend="vault", credential_version_id="password=plain-secret",
        )["saved"])
        self.assertEqual(manager.verify()["event_count"], 1)

    def test_hash_chain_detects_record_tampering(self):
        manager = self.manager()
        self.record_route(manager)
        data = self.path.read_text(encoding="utf-8")
        self.path.write_text(data.replace('"reason_code":"ROUTED"', '"reason_code":"ALTERED"'), encoding="utf-8")
        result = manager.verify()
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "AUDIT_LOG_INTEGRITY_FAILED")
        self.assertEqual(manager.read_events()["events"], [])

    def test_hash_chain_detects_reordering_and_incomplete_final_record(self):
        manager = self.manager()
        self.record_route(manager, 1)
        self.record_route(manager, 2)
        lines = self.path.read_bytes().splitlines(keepends=True)
        self.path.write_bytes(lines[1] + lines[0])
        self.assertFalse(manager.verify()["valid"])
        self.path.write_bytes(lines[0].rstrip(b"\n"))
        self.assertFalse(manager.verify()["valid"])

    def test_wrong_signing_key_cannot_read_or_extend_log(self):
        self.record_route(self.manager())
        other = ProviderAuditLog(self.path, signing_key=b"wrong-signing-key-material-32-bytes!!")
        self.assertFalse(other.verify()["valid"])
        self.assertFalse(self.record_route(other, 3)["saved"])

    def test_capacity_and_read_limit_are_validated(self):
        manager = self.manager(max_events=1)
        self.assertTrue(self.record_route(manager)["saved"])
        self.assertEqual(self.record_route(manager, 2)["status"], "AUDIT_CAPACITY_REACHED")
        self.assertEqual(manager.read_events(limit=0)["status"], "INVALID_AUDIT_READ_LIMIT")

    def test_multiple_manager_instances_append_under_thread_contention(self):
        managers = [self.manager() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda index: self.record_route(managers[index % len(managers)], index + 1),
                range(24),
            ))
        self.assertTrue(all(item["saved"] for item in results))
        check = self.manager().verify()
        self.assertTrue(check["valid"])
        self.assertEqual(check["event_count"], 24)

    def test_multiple_processes_append_without_lost_or_reordered_events(self):
        args = [(str(self.path), self.key, index) for index in range(12)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_append_audit_event, args))
        self.assertTrue(all(results))
        verified = self.manager().verify()
        self.assertTrue(verified["valid"])
        self.assertEqual(verified["event_count"], 12)
        events = self.manager().read_events(limit=100)["events"]
        self.assertEqual(len({event["request_ref"] for event in events}), 12)

    def test_invalid_signing_key_is_rejected(self):
        with self.assertRaises(ValueError):
            ProviderAuditLog(self.path, signing_key=b"too-short")


if __name__ == "__main__":
    unittest.main()
