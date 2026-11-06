import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.provider_audit_log import ProviderAuditLog


class ProviderAuditLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "provider-audit.json"
        self.key = b"test-only-integrity-key-with-32-bytes-minimum"
        self.clock_value = 1000.0
        self.log = ProviderAuditLog(
            self.path, integrity_key=self.key, clock=lambda: self.clock_value,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_append_verify_and_reload_preserves_hash_chain(self):
        first = self.log.append(
            "authorization_decision", actor_id="admin-1", provider_id="image",
            outcome="allowed", details={"operation": "provider.configure", "status": "ALLOWED"},
        )
        self.clock_value += 1
        second = self.log.append(
            "routing_decision", actor_id="admin-1", tenant_id="tenant-a",
            provider_id="image", request_id="request-1", trace_id="trace-1",
            outcome="success", details={"operation": "route_request", "billing_mode": "ZERO_COST"},
        )
        self.assertTrue(first["saved"])
        self.assertTrue(second["saved"])
        self.assertEqual(self.log.verify(), {
            "valid": True, "status": "AUDIT_CHAIN_VALID", "events": 2,
        })
        reloaded = ProviderAuditLog(self.path, integrity_key=self.key)
        page = reloaded.read_events(limit=1)
        self.assertEqual(page["events"][0]["sequence"], 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(reloaded.read_events(limit=2, after_sequence=1)["events"][0]["sequence"], 2)

    def test_modified_event_fails_hmac_and_chain_verification(self):
        self.log.append(
            "credential_rotation", actor_id="admin-1", provider_id="image",
            outcome="success", details={"operation": "provider.credentials.rotate"},
        )
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["events"][0]["outcome"] = "failure"
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.log.verify()["status"], "AUDIT_INTEGRITY_FAILURE")
        self.assertEqual(
            self.log.append("routing_decision", outcome="attempted")["status"],
            "AUDIT_INTEGRITY_FAILURE",
        )

    def test_malformed_unhashable_event_type_is_reported_as_integrity_failure(self):
        self.path.write_text(
            json.dumps({"version": 1, "events": [{
                "sequence": 1, "timestamp": 1.0, "event_type": [],
                "actor_id": None, "tenant_id": None, "provider_id": None,
                "request_id": None, "trace_id": None, "outcome": "success",
                "details": {}, "previous_hash": "0" * 64, "event_hash": "0" * 64,
            }]}),
            encoding="utf-8",
        )
        self.assertEqual(self.log.verify()["status"], "AUDIT_INTEGRITY_FAILURE")

    def test_previous_hash_tampering_is_detected(self):
        self.log.append("routing_decision", outcome="attempted")
        self.log.append("routing_decision", outcome="success")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["events"][1]["previous_hash"] = "f" * 64
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertFalse(self.log.verify()["valid"])

    def test_credentials_and_unapproved_payload_fields_are_never_accepted(self):
        rejected = self.log.append(
            "routing_decision", outcome="success",
            details={"prompt": "private user prompt"},
        )
        self.assertFalse(rejected["saved"])
        self.assertEqual(rejected["status"], "AUDIT_EVENT_INVALID")
        rejected_secret = self.log.append(
            "credential_rotation", outcome="success",
            details={"operation": "provider.credentials.rotate", "token": "secret-value"},
        )
        self.assertFalse(rejected_secret["saved"])
        self.assertNotIn("secret-value", self.path.read_text(encoding="utf-8"))

    def test_credential_shaped_identifier_and_approval_id_are_rejected(self):
        self.assertEqual(
            self.log.append(
                "authorization_decision", actor_id="Bearer super-secret",
                outcome="denied",
            )["status"],
            "AUDIT_EVENT_INVALID",
        )
        self.assertEqual(
            self.log.append(
                "approval_decision", outcome="allowed",
                details={"approval_id": "token=private-value"},
            )["status"],
            "AUDIT_EVENT_INVALID",
        )
        self.assertNotIn("private-value", self.path.read_text(encoding="utf-8"))

    def test_model_labels_with_spaces_are_supported_without_relaxing_secret_checks(self):
        result = self.log.append(
            "provider_configuration", outcome="success",
            details={"operation": "provider.configure", "model": "Stable Diffusion XL"},
        )
        self.assertTrue(result["saved"])
        self.assertEqual(
            self.log.read_events(limit=1)["events"][0]["details"]["model"],
            "Stable Diffusion XL",
        )

    def test_event_types_and_outcomes_are_allowlisted(self):
        self.assertEqual(
            self.log.append("unknown_event", outcome="success")["status"],
            "AUDIT_EVENT_TYPE_INVALID",
        )
        self.assertEqual(
            self.log.append("routing_decision", outcome="maybe")["status"],
            "AUDIT_OUTCOME_INVALID",
        )

    def test_identifiers_and_details_are_bounded(self):
        self.assertEqual(
            self.log.append("routing_decision", actor_id="x" * 201, outcome="success")["status"],
            "AUDIT_EVENT_INVALID",
        )
        self.assertEqual(
            self.log.append("routing_decision", outcome="success", details={"status": "x" * 161})["status"],
            "AUDIT_EVENT_INVALID",
        )

    def test_key_must_be_strong_and_external(self):
        with self.assertRaises(ValueError):
            ProviderAuditLog(self.path.parent / "weak.json", integrity_key=b"short")

    def test_pagination_rejects_invalid_ranges(self):
        self.assertEqual(self.log.read_events(limit=0)["status"], "INVALID_AUDIT_PAGE")
        self.assertEqual(self.log.read_events(after_sequence=-1)["status"], "INVALID_AUDIT_PAGE")

    def test_multiple_instances_append_without_losing_prior_events(self):
        other = ProviderAuditLog(self.path, integrity_key=self.key)
        self.log.append("routing_decision", request_id="request-a", outcome="attempted")
        other.append("routing_decision", request_id="request-b", outcome="success")
        self.assertEqual(self.log.verify()["events"], 2)
        self.assertEqual(other.read_events(limit=10)["events"][1]["request_id"], "request-b")


if __name__ == "__main__":
    unittest.main()
