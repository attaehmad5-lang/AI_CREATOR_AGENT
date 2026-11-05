"""Tamper-evident, concurrent and secret-safe provider audit-log tests."""
import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLogManager


def _append_audit_in_process(args):
    log_path, index = args
    manager = ProviderAuditLogManager(log_path)
    return manager.record_event(
        "ROUTING_DECISION",
        outcome="ALLOWED",
        status_code="ROUTE_SELECTED",
        actor_principal_id=f"principal-{index}",
        tenant_id="tenant-1",
        project_id="project-1",
        provider_id="provider-1",
        request_id=f"request-{index}",
        metadata={"category": "image", "strategy": "ROUND_ROBIN", "candidate_count": 3},
    )["saved"]


class ProviderAuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "provider-audit.ndjson"
        self.now = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        self.manager = ProviderAuditLogManager(
            self.path, clock=lambda: self.now, max_events=1000
        )

    def append(self, **kwargs):
        defaults = {
            "event_type": "ROUTING_DECISION",
            "outcome": "ALLOWED",
            "status_code": "ROUTE_SELECTED",
            "actor_principal_id": "principal-1",
            "tenant_id": "tenant-1",
            "project_id": "project-1",
            "provider_id": "image",
            "request_id": "request-1",
            "metadata": {"category": "image", "strategy": "LOWEST_COST"},
        }
        defaults.update(kwargs)
        event_type = defaults.pop("event_type")
        return self.manager.record_event(event_type, **defaults)

    def test_records_are_append_only_hash_chained_and_read_back_verified(self):
        first = self.append()
        second = self.append(
            event_type="CREDENTIAL_ROTATION",
            outcome="SUCCEEDED",
            status_code="SECRET_ROTATED",
            metadata={"credential_backend": "vault", "credential_present": True},
        )
        self.assertTrue(first["saved"])
        self.assertTrue(second["saved"])
        events = self.manager.read_events(limit=10)
        self.assertTrue(events["verified"])
        self.assertEqual([item["sequence"] for item in events["events"]], [1, 2])
        self.assertEqual(events["events"][1]["previous_hash"], first["event_hash"])
        self.assertEqual(self.manager.verify_integrity()["status"], "AUDIT_CHAIN_VALID")

    def test_tampered_middle_record_and_truncated_tail_fail_closed(self):
        self.append(request_id="request-1")
        self.append(request_id="request-2")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[0])
        event["outcome"] = "DENIED"
        lines[0] = json.dumps(event, sort_keys=True, separators=(",", ":"))
        self.path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        self.assertFalse(self.manager.verify_integrity()["valid"])
        blocked = self.append(request_id="request-3")
        self.assertFalse(blocked["saved"])
        self.assertEqual(blocked["status"], "AUDIT_LOG_CHAIN_TAMPERED")

        self.path.write_text('{"incomplete":', encoding="utf-8")
        self.assertEqual(self.manager.verify_integrity()["status"], "AUDIT_LOG_TRUNCATED")

    def test_rejects_unapproved_metadata_keys_and_secret_markers(self):
        unsafe = self.append(metadata={"api_key": "secret"})
        self.assertEqual(unsafe["status"], "UNSAFE_AUDIT_METADATA")
        unsafe_value = self.append(metadata={"model": "Bearer leaked-token"})
        self.assertEqual(unsafe_value["status"], "UNSAFE_AUDIT_METADATA")
        self.assertFalse(self.path.exists())

    def test_rejects_invalid_event_status_and_identifiers(self):
        self.assertEqual(
            self.manager.record_event([], outcome="ALLOWED", status_code="OK")["status"],
            "INVALID_AUDIT_EVENT_TYPE",
        )
        self.assertEqual(
            self.manager.record_event("ROUTING_DECISION", outcome=[], status_code="OK")["status"],
            "INVALID_AUDIT_OUTCOME",
        )
        self.assertEqual(
            self.manager.record_event("ROUTING_DECISION", outcome="ALLOWED", status_code="BAD CODE")["status"],
            "INVALID_AUDIT_STATUS_CODE",
        )
        self.assertEqual(
            self.manager.record_event(
                "ROUTING_DECISION", outcome="ALLOWED", status_code="OK",
                request_id="bad id",
            )["status"],
            "INVALID_AUDIT_REQUEST_ID",
        )
        self.assertFalse(self.path.exists())

    def test_invalid_clock_and_capacity_fail_closed(self):
        invalid_clock = ProviderAuditLogManager(
            self.path, clock=lambda: datetime(2026, 1, 1)
        )
        self.assertEqual(
            invalid_clock.record_event("ROUTING_DECISION", outcome="ALLOWED", status_code="OK")["status"],
            "INVALID_AUDIT_CLOCK",
        )
        limited = ProviderAuditLogManager(self.path, clock=lambda: self.now, max_events=1)
        self.assertTrue(limited.record_event("ROUTING_DECISION", outcome="ALLOWED", status_code="OK")["saved"])
        self.assertEqual(
            limited.record_event("ROUTING_DECISION", outcome="ALLOWED", status_code="OK")["status"],
            "AUDIT_LOG_EVENT_CAPACITY_EXCEEDED",
        )

    def test_tenant_filter_and_pagination_are_applied_to_verified_chain(self):
        self.append(request_id="req-1", tenant_id="tenant-1", project_id="project-1")
        self.append(request_id="req-2", tenant_id="tenant-2", project_id="project-1")
        self.append(request_id="req-3", tenant_id="tenant-1", project_id="project-1")
        self.append(request_id="req-4", tenant_id="tenant-1", project_id="project-2")
        page = self.manager.read_events(
            after_sequence=0, limit=1, tenant_id="tenant-1", project_id="project-1"
        )
        self.assertEqual([item["request_id"] for item in page["events"]], ["req-1"])
        self.assertTrue(page["has_more"])
        next_page = self.manager.read_events(
            after_sequence=page["next_sequence"], limit=10,
            tenant_id="tenant-1", project_id="project-1",
        )
        self.assertEqual([item["request_id"] for item in next_page["events"]], ["req-3"])
        project_two = self.manager.read_events(
            after_sequence=0, limit=10, tenant_id="tenant-1", project_id="project-2"
        )
        self.assertEqual([item["request_id"] for item in project_two["events"]], ["req-4"])
        self.assertEqual(
            self.manager.read_events(limit=10, project_id="invalid project")["status"],
            "INVALID_AUDIT_PROJECT_ID",
        )
        self.assertEqual(self.manager.read_events(limit=0)["status"], "INVALID_AUDIT_LIMIT")

    def test_concurrent_threads_preserve_sequence_and_hash_links(self):
        manager = ProviderAuditLogManager(self.path, clock=lambda: self.now, max_events=1000)

        def write(index):
            return manager.record_event(
                "BILLING_APPROVAL",
                outcome="ALLOWED",
                status_code="PAID_APPROVAL_VERIFIED",
                actor_principal_id=f"principal-{index}",
                tenant_id="tenant-1",
                project_id="project-1",
                provider_id="provider-1",
                request_id=f"thread-{index}",
                metadata={"approval_required": True, "approval_granted": True, "estimated_cost": 1.25, "cost_unit": "USD"},
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(write, range(60)))
        self.assertTrue(all(item["saved"] for item in results))
        report = manager.verify_integrity()
        self.assertTrue(report["valid"])
        self.assertEqual(report["event_count"], 60)

    def test_concurrent_processes_preserve_a_single_hash_chain(self):
        args = [(str(self.path), index) for index in range(12)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_append_audit_in_process, args))
        self.assertTrue(all(results))
        report = ProviderAuditLogManager(self.path).verify_integrity()
        self.assertTrue(report["valid"])
        self.assertEqual(report["event_count"], 12)

    def test_new_nested_log_path_creates_directories_safely(self):
        nested_path = Path(self.temp.name) / "audit" / "events" / "provider.ndjson"
        manager = ProviderAuditLogManager(nested_path)
        result = manager.record_event(
            "ROUTING_DECISION",
            outcome="ALLOWED",
            status_code="ROUTE_SELECTED",
            project_id="project-1",
        )
        self.assertTrue(result["saved"])
        self.assertTrue(nested_path.is_file())
        self.assertTrue(manager.verify_integrity()["valid"])
        self.assertTrue(manager.read_events()["verified"])

    def test_log_contains_only_explicit_audit_fields_not_provider_payloads(self):
        self.append(metadata={"category": "image", "model": "safe-model"})
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("api_key", raw.casefold())
        self.assertNotIn("payload", raw.casefold())
        self.assertNotIn("credential_value", raw.casefold())
        self.assertIn("safe-model", raw)


if __name__ == "__main__":
    unittest.main()
