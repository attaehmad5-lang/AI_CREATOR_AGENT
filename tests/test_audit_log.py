import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.audit_log import ProviderAuditLog


class ProviderAuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "provider-audit.jsonl"
        self.log = ProviderAuditLog(self.path, max_file_bytes=100_000)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_records_allowlisted_event_without_identity_or_secret_payload(self):
        result = self.log.record({
            "event_type": "authorization", "decision": "allowed",
            "action": "provider.route", "status": "AUTHORIZED",
            "project_id": "film", "provider_id": "image",
        })
        self.assertTrue(result["saved"])
        record = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "provider.route")
        self.assertEqual(record["project_id"], "film")
        self.assertNotIn("subject", record)
        self.assertNotIn("token", record)

    def test_arbitrary_fields_and_secret_like_values_are_rejected(self):
        bad_fields = self.log.record({
            "event_type": "authorization", "decision": "denied",
            "action": "provider.route", "status": "DENIED",
            "token": "secret-value",
        })
        self.assertFalse(bad_fields["saved"])
        secret_value = self.log.record({
            "event_type": "authorization", "decision": "denied",
            "action": "provider.route", "status": "token=secret",
        })
        self.assertFalse(secret_value["saved"])
        self.assertFalse(self.path.exists())

    def test_concurrent_events_are_complete_json_lines(self):
        events = [{
            "event_type": "routing", "decision": "success",
            "action": "provider.route", "status": "ROUTED",
            "project_id": "film", "provider_id": f"image-{i}",
            "request_id": f"request-{i}",
        } for i in range(60)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(self.log.record, events))
        self.assertTrue(all(item["saved"] for item in results))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 60)
        parsed = [json.loads(line) for line in lines]
        self.assertEqual(len({item["request_id"] for item in parsed}), 60)

    def test_capacity_limit_fails_closed_without_truncating_existing_audit(self):
        log = ProviderAuditLog(self.path, max_file_bytes=1024, max_event_bytes=512)
        event = {
            "event_type": "authorization", "decision": "denied",
            "action": "provider.route", "status": "PERMISSION_DENIED",
            "project_id": "film",
        }
        statuses = []
        for _ in range(20):
            statuses.append(log.record(event)["status"])
        self.assertIn("AUDIT_LOG_CAPACITY_REACHED", statuses)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines)
        self.assertLessEqual(self.path.stat().st_size, 1024)

    def test_malformed_event_never_raises(self):
        self.assertEqual(self.log.record([])["status"], "INVALID_AUDIT_EVENT")
        self.assertEqual(self.log.record({
            "event_type": [], "decision": {}, "action": "provider.route", "status": "DENIED"
        })["status"], "INVALID_AUDIT_EVENT")


if __name__ == "__main__":
    unittest.main()
