import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLog


class ProviderAuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "audit.json"
        self.now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
        self.audit = ProviderAuditLog(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.temp_dir.cleanup()

    def append(self, **kwargs):
        defaults = {
            "actor_id": "user-1",
            "operation": "provider.generate",
            "resource_type": "provider",
            "resource_id": "image",
            "outcome": "ALLOWED",
            "request_id": "request-1",
        }
        defaults.update(kwargs)
        return self.audit.append(**defaults)

    def test_hash_chain_is_append_only_and_verifiable_after_reload(self):
        first = self.append()
        second = self.append(
            operation="provider.verify", outcome="DENIED", request_id="request-2"
        )
        self.assertTrue(first["saved"])
        self.assertEqual(second["sequence"], 2)
        reloaded = ProviderAuditLog(self.path, clock=lambda: self.now)
        chain = reloaded.verify_chain()
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["entry_count"], 2)
        self.assertEqual(
            reloaded.data["entries"][1]["previous_hash"],
            reloaded.data["entries"][0]["entry_hash"],
        )

    def test_tampering_with_a_persisted_event_is_detected(self):
        self.append()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["entries"][0]["outcome"] = "BYPASSED"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderAuditLog(self.path, clock=lambda: self.now)

    def test_audit_log_capacity_fails_closed_without_overwriting_history(self):
        audit = ProviderAuditLog(self.path, max_entries=1, clock=lambda: self.now)
        self.assertTrue(self.append().__class__ is dict)
        full = audit.append(
            actor_id="user-2", operation="provider.generate",
            resource_type="provider", resource_id="image", outcome="ALLOWED",
        )
        self.assertEqual(full["status"], "AUDIT_LOG_FULL")
        self.assertEqual(len(audit.data["entries"]), 1)

    def test_raw_payloads_are_not_accepted_and_identifiers_are_validated(self):
        invalid = self.append(actor_id="user with spaces")
        self.assertEqual(invalid["status"], "INVALID_AUDIT_ACTOR")
        invalid_request = self.append(request_id="bad request id")
        self.assertEqual(invalid_request["status"], "INVALID_AUDIT_REQUEST_ID")
        self.assertNotIn("prompt", self.path.read_text(encoding="utf-8"))

    def test_invalid_capacity_and_naive_clock_fail_closed(self):
        with self.assertRaises(ValueError):
            ProviderAuditLog(self.path, max_entries=True)
        audit = ProviderAuditLog(self.path, clock=lambda: datetime(2026, 10, 17, 12))
        result = audit.append(
            actor_id="user-1", operation="provider.generate",
            resource_type="provider", resource_id="image", outcome="ALLOWED",
        )
        self.assertEqual(result["status"], "INVALID_AUDIT_CLOCK")


if __name__ == "__main__":
    unittest.main()
