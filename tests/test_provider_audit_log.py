import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLog


class ProviderAuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "audit.jsonl"
        self.clock = lambda: datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        self.audit = ProviderAuditLog(self.path, clock=self.clock)

    def tearDown(self):
        self.temp.cleanup()

    def test_records_are_allowlisted_hash_chained_and_verifiable(self):
        first = self.audit.record(
            "provider.verify", status="CONNECTED", outcome="OK",
            provider_id="image", actor_id="user-1", request_id="request-1",
        )
        second = self.audit.record(
            "provider.execute", status="EXECUTION_FAILED", outcome="ERROR",
            provider_id="image", request_id="request-2", reason_code="OUTPUT_INVALID",
        )
        self.assertTrue(first["saved"])
        self.assertTrue(second["saved"])
        chain = self.audit.verify_chain()
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["count"], 2)
        lines = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[1]["previous_hash"], lines[0]["record_hash"])
        self.assertNotIn("payload", json.dumps(lines))
        self.assertNotIn("credentials", json.dumps(lines))

    def test_invalid_fields_do_not_write_audit_records(self):
        for kwargs in (
            {"event": "Bad Event", "status": "OK", "outcome": "OK"},
            {"event": "provider.execute", "status": "BAD STATUS", "outcome": "OK"},
            {"event": "provider.execute", "status": "OK", "outcome": "MAYBE"},
            {"event": "provider.execute", "status": "OK", "outcome": "OK", "request_id": "bad request"},
        ):
            with self.subTest(kwargs=kwargs):
                result = self.audit.record(**kwargs)
                self.assertFalse(result["saved"])
        self.assertFalse(self.path.exists())

    def test_tampered_record_breaks_chain_and_prevents_future_append(self):
        self.audit.record(
            "provider.verify", status="CONNECTED", outcome="OK", provider_id="image"
        )
        record = json.loads(self.path.read_text(encoding="utf-8"))
        record["status"] = "ADMIN_APPROVED"
        self.path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        check = self.audit.verify_chain()
        self.assertFalse(check["valid"])
        result = self.audit.record(
            "provider.execute", status="EXECUTION_SUCCEEDED", outcome="OK", provider_id="image"
        )
        self.assertFalse(result["saved"])
        self.assertEqual(result["status"], "AUDIT_LOG_CHAIN_INVALID")

    def test_truncated_log_and_symlink_are_rejected(self):
        self.path.write_bytes(b'{"partial":')
        self.assertEqual(self.audit.verify_chain()["status"], "AUDIT_LOG_TRUNCATED")
        self.path.unlink()
        target = self.root / "target.jsonl"
        target.write_text("", encoding="utf-8")
        link = self.root / "linked.jsonl"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        audit = ProviderAuditLog(link)
        result = audit.record("provider.verify", status="CONNECTED", outcome="OK")
        self.assertEqual(result["status"], "AUDIT_LOG_SYMLINK_REJECTED")

    def test_concurrent_writers_preserve_a_single_valid_hash_chain(self):
        def write(index):
            return self.audit.record(
                "provider.execute",
                status="EXECUTION_SUCCEEDED",
                outcome="OK",
                provider_id="image",
                request_id=f"concurrent-{index}",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(40)))
        self.assertTrue(all(item["saved"] for item in results))
        chain = self.audit.verify_chain()
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["count"], 40)
        records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len({item["request_id"] for item in records}), 40)

    def test_size_limit_is_enforced_without_truncating_existing_audit(self):
        audit = ProviderAuditLog(self.path, max_bytes=1024, clock=self.clock)
        result = audit.record(
            "provider.execute", status="EXECUTION_SUCCEEDED", outcome="OK",
            request_id="x" * 190,
        )
        self.assertTrue(result["saved"])
        original = self.path.read_bytes()
        for index in range(10):
            audit.record(
                "provider.execute", status="EXECUTION_SUCCEEDED", outcome="OK",
                request_id=("request-" + str(index)),
            )
        if self.path.stat().st_size >= 1024:
            self.assertLessEqual(self.path.stat().st_size, 1024)
        self.assertTrue(self.path.read_bytes().startswith(original[:20]))


if __name__ == "__main__":
    unittest.main()
