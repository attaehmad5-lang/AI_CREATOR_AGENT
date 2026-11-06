"""Provider audit log integrity and concurrency regression tests."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLog


class ProviderAuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "audit.json"
        self.log = ProviderAuditLog(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def append(self, **overrides):
        values = dict(
            event_type="ROUTING_DECISION", outcome="ROUTED",
            operation="provider.generate", actor_id="user-1",
            tenant_id="tenant-a", project_id="film", request_id="request-1",
            provider_id="huggingface", billing_mode="ZERO_COST", timestamp=1000,
        )
        values.update(overrides)
        return self.log.append(**values)

    def test_records_are_hash_chained_and_do_not_store_unapproved_fields(self):
        first = self.append()
        second = self.append(
            event_type="CREDENTIAL_ACCESS", outcome="GRANTED",
            operation="provider.credentials.read", request_id="request-2",
        )
        self.assertTrue(first["saved"])
        self.assertTrue(second["saved"])
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["events"]), 2)
        self.assertEqual(state["events"][0]["previous_hash"], "0" * 64)
        self.assertEqual(
            state["events"][1]["previous_hash"], state["events"][0]["event_hash"]
        )
        self.assertEqual(
            set(state["events"][0]),
            {
                "sequence", "timestamp", "event_type", "actor_id", "tenant_id",
                "project_id", "request_id", "provider_id", "operation", "outcome",
                "billing_mode", "previous_hash", "event_hash",
            },
        )
        self.assertTrue(self.log.verify()["valid"])

    def test_tampering_is_detected_and_future_appends_fail_closed(self):
        self.append()
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["events"][0]["provider_id"] = "other-provider"
        self.path.write_text(json.dumps(state), encoding="utf-8")
        verified = self.log.verify()
        self.assertFalse(verified["valid"])
        denied = self.append(request_id="after-tamper")
        self.assertFalse(denied["saved"])
        self.assertEqual(denied["status"], "AUDIT_LOG_INTEGRITY_FAILURE")

    def test_invalid_or_secret_like_values_are_rejected_before_persistence(self):
        invalid = self.append(provider_id="api_key:SECRET")
        self.assertEqual(invalid["status"], "INVALID_AUDIT_IDENTIFIER")
        self.assertFalse(self.path.exists())
        for event_type in ("UNKNOWN", "routing"):
            result = self.append(event_type=event_type)
            self.assertEqual(result["status"], "INVALID_AUDIT_EVENT_TYPE")
        invalid_outcome = self.append(outcome="routing succeeded")
        self.assertEqual(invalid_outcome["status"], "INVALID_AUDIT_OUTCOME")
        invalid_mode = self.append(billing_mode="FREE_TEXT")
        self.assertEqual(invalid_mode["status"], "INVALID_AUDIT_BILLING_MODE")

    def test_tenant_filter_and_query_limits_are_validated(self):
        self.append(tenant_id="tenant-a")
        self.append(tenant_id="tenant-b", request_id="request-2")
        rows = self.log.list_events(tenant_id="tenant-a")
        self.assertEqual(rows["event_count"], 1)
        self.assertEqual(rows["events"][0]["tenant_id"], "tenant-a")
        self.assertEqual(
            self.log.list_events(limit=True)["status"], "INVALID_AUDIT_QUERY_LIMIT"
        )
        self.assertEqual(
            self.log.list_events(event_type="NOT_REAL")["status"], "INVALID_AUDIT_EVENT_TYPE"
        )

    def test_concurrent_instances_do_not_lose_audit_events(self):
        instances = [ProviderAuditLog(self.path) for _ in range(6)]
        def write(index):
            return instances[index % len(instances)].append(
                event_type="ROUTING_DECISION", outcome="ROUTED",
                operation="provider.generate", request_id=f"request-{index}",
                provider_id="huggingface", timestamp=1000 + index,
            )
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(write, range(48)))
        self.assertTrue(all(item["saved"] for item in results))
        self.assertTrue(self.log.verify()["valid"])
        self.assertEqual(self.log.verify()["event_count"], 48)

    def test_empty_log_verifies_and_invalid_inputs_fail_closed(self):
        self.assertTrue(self.log.verify()["valid"])
        self.assertEqual(self.append(timestamp=True)["status"], "INVALID_AUDIT_TIMESTAMP")
        self.assertEqual(
            self.append(operation="Provider.Generate")["status"], "INVALID_AUDIT_OPERATION"
        )
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
