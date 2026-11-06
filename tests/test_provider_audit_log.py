"""Tamper-evident provider audit log integrity, privacy and concurrency tests."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLog


class ProviderAuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "provider-audit.json"
        self.key = b"test-only-audit-signing-key-32-bytes-minimum"

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_manager(self, *, max_events=1000, key=None):
        return ProviderAuditLog(
            self.path,
            signing_key=self.key if key is None else key,
            key_id="test-primary",
            max_events=max_events,
        )

    def test_append_verifies_hmac_chain_and_pseudonymizes_actor(self):
        manager = self.make_manager()
        result = manager.record(
            operation="provider.credentials.rotate",
            outcome="PROVIDER_SECRET_ROTATED",
            principal_id="opaque-principal-123",
            provider_id="image-provider",
            tenant_id="tenant-a",
            project_id="documentary",
            request_id="rotate-request-001",
            trace_id="a" * 32,
        )
        self.assertTrue(result["saved"])
        integrity = manager.verify_integrity()
        self.assertTrue(integrity["valid"])
        self.assertEqual(integrity["event_count"], 1)
        event = manager.read_events()["events"][0]
        self.assertEqual(event["operation"], "provider.credentials.rotate")
        self.assertEqual(event["outcome"], "PROVIDER_SECRET_ROTATED")
        self.assertRegex(event["actor_ref"], r"^[0-9a-f]{64}$")
        self.assertNotIn("opaque-principal-123", repr(event))
        self.assertNotIn(self.key.decode("utf-8"), self.path.read_text(encoding="utf-8"))

    def test_event_payloads_and_unknown_fields_are_not_accepted(self):
        manager = self.make_manager()
        with self.assertRaises(TypeError):
            manager.record(
                operation="provider.generate",
                outcome="STARTED",
                prompt="private user prompt",
            )
        self.assertEqual(manager.verify_integrity()["event_count"], 0)

    def test_invalid_identifiers_and_trace_ids_fail_before_write(self):
        manager = self.make_manager()
        self.assertEqual(
            manager.record(operation="Provider.Generate", outcome="STARTED")["status"],
            "INVALID_AUDIT_OPERATION",
        )
        self.assertEqual(
            manager.record(operation="provider.generate", outcome="started")["status"],
            "INVALID_AUDIT_OUTCOME",
        )
        self.assertEqual(
            manager.record(
                operation="provider.generate", outcome="STARTED",
                trace_id="not-a-trace",
            )["status"],
            "INVALID_AUDIT_TRACE_ID",
        )
        self.assertEqual(manager.verify_integrity()["event_count"], 0)

    def test_tampered_event_is_detected_and_append_is_blocked(self):
        manager = self.make_manager()
        manager.record(
            operation="provider.configure", outcome="PROVIDER_CONFIGURED",
            provider_id="image-provider",
        )
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        stored["events"][0]["outcome"] = "PROVIDER_DELETED"
        self.path.write_text(json.dumps(stored), encoding="utf-8")

        result = manager.verify_integrity()
        self.assertFalse(result["valid"])
        self.assertEqual(result["status"], "AUDIT_LOG_INTEGRITY_FAILED")
        with self.assertRaisesRegex(ValueError, "integrity verification failed"):
            manager.record(operation="provider.configure", outcome="PROVIDER_CONFIGURED")

    def test_wrong_signing_key_cannot_open_existing_audit_history(self):
        manager = self.make_manager()
        manager.record(operation="provider.verify", outcome="VERIFIED")
        with self.assertRaisesRegex(ValueError, "integrity verification failed"):
            ProviderAuditLog(
                self.path,
                signing_key=b"incorrect-audit-key-32-bytes-long!!!",
                key_id="test-primary",
            )

    def test_read_filters_and_cursor_are_bounded(self):
        manager = self.make_manager()
        manager.record(operation="provider.verify", outcome="VERIFIED", provider_id="image-a")
        manager.record(operation="provider.verify", outcome="FAILED", provider_id="video-a")
        manager.record(operation="provider.configure", outcome="UPDATED", provider_id="image-a")
        page = manager.read_events(after_sequence=0, limit=1, provider_id="image-a")
        self.assertEqual(len(page["events"]), 1)
        self.assertTrue(page["has_more"])
        next_page = manager.read_events(after_sequence=1, limit=10, provider_id="image-a")
        self.assertEqual(len(next_page["events"]), 1)
        self.assertEqual(next_page["events"][0]["operation"], "provider.configure")
        self.assertEqual(manager.read_events(limit=501)["status"], "INVALID_AUDIT_LIMIT")

    def test_capacity_is_enforced_without_overwriting_previous_events(self):
        manager = self.make_manager(max_events=2)
        self.assertTrue(manager.record(operation="provider.verify", outcome="ONE")["saved"])
        self.assertTrue(manager.record(operation="provider.verify", outcome="TWO")["saved"])
        third = manager.record(operation="provider.verify", outcome="THREE")
        self.assertEqual(third["status"], "AUDIT_CAPACITY_REACHED")
        self.assertEqual(manager.verify_integrity()["event_count"], 2)

    def test_concurrent_writers_across_manager_instances_do_not_lose_events(self):
        managers = [self.make_manager(), self.make_manager()]
        def append(index):
            manager = managers[index % 2]
            return manager.record(
                operation="provider.verify",
                outcome="VERIFIED",
                provider_id=f"provider-{index}",
                request_id=f"request-{index}",
            )
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(append, range(40)))
        self.assertTrue(all(item["saved"] for item in results))
        integrity = managers[0].verify_integrity()
        self.assertTrue(integrity["valid"])
        self.assertEqual(integrity["event_count"], 40)

    def test_key_id_mismatch_fails_closed(self):
        self.make_manager().record(operation="provider.verify", outcome="VERIFIED")
        with self.assertRaisesRegex(ValueError, "signing key mismatch"):
            ProviderAuditLog(
                self.path,
                signing_key=self.key,
                key_id="rotated-key",
            )


if __name__ == "__main__":
    unittest.main()
