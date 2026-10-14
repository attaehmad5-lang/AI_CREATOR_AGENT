import json
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.execution_ledger import ProviderExecutionLedger


TRACE_A = "0123456789abcdef0123456789abcdef"
TRACE_B = "1123456789abcdef0123456789abcdef"


def _reserve_execution_in_process(args):
    path, request_id = args
    ledger = ProviderExecutionLedger(path)
    fingerprint = ledger.fingerprint_request(
        project_id="film", category="image", estimated_cost=1.0,
        routing_options={"model": "image-v1"},
    )
    return ledger.reserve(
        request_id=request_id, project_id="film", category="image",
        estimated_cost=1.0, trace_id=TRACE_A, fingerprint=fingerprint,
    )


class ProviderExecutionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "execution.json"
        self.ledger = ProviderExecutionLedger(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def fingerprint(self, *, project_id="film", category="image", cost=1.0, options=None):
        return self.ledger.fingerprint_request(
            project_id=project_id, category=category,
            estimated_cost=cost, routing_options=options or {},
        )

    def reserve(self, request_id="request-1", *, trace_id=TRACE_A, fingerprint=None):
        return self.ledger.reserve(
            request_id=request_id, project_id="film", category="image",
            estimated_cost=1.0, trace_id=trace_id,
            fingerprint=fingerprint or self.fingerprint(),
        )

    def test_reserve_persists_a_request_claim(self):
        result = self.reserve()
        self.assertEqual(result["status"], "RESERVED")
        self.assertTrue(result["reserved"])
        loaded = ProviderExecutionLedger(self.path)
        record = loaded.get("request-1")["request"]
        self.assertEqual(record["status"], "RESERVED")
        self.assertEqual(record["trace_id"], TRACE_A)

    def test_duplicate_active_request_is_blocked(self):
        self.assertTrue(self.reserve()["reserved"])
        duplicate = self.reserve(trace_id=TRACE_B)
        self.assertEqual(duplicate["status"], "REQUEST_IN_PROGRESS")
        self.assertFalse(duplicate["reserved"])
        self.assertEqual(duplicate["trace_id"], TRACE_A)

    def test_started_request_replay_is_reported_as_unknown(self):
        self.reserve()
        self.assertTrue(self.ledger.mark_started(request_id="request-1", provider_id="image")["saved"])
        result = self.reserve(trace_id=TRACE_B)
        self.assertEqual(result["status"], "REQUEST_OUTCOME_UNKNOWN")
        self.assertFalse(result["reserved"])

    def test_terminal_request_replay_returns_duplicate_without_reserving(self):
        self.reserve()
        self.ledger.mark_started(request_id="request-1", provider_id="image")
        self.assertTrue(self.ledger.finish(
            request_id="request-1", status="COMPLETED",
            result_status="EXECUTION_SUCCEEDED", provider_id="image",
        )["saved"])
        result = self.reserve(trace_id=TRACE_B)
        self.assertEqual(result["status"], "DUPLICATE_REQUEST")
        self.assertEqual(result["existing_status"], "COMPLETED")
        self.assertEqual(result["result_status"], "EXECUTION_SUCCEEDED")
        self.assertTrue(result["executed"])
        self.assertEqual(result["trace_id"], TRACE_A)

    def test_reusing_request_id_for_different_fingerprint_is_conflict(self):
        self.reserve()
        different = self.fingerprint(cost=2.0)
        result = self.reserve(fingerprint=different)
        self.assertEqual(result["status"], "REQUEST_ID_CONFLICT")
        self.assertFalse(result["reserved"])

    def test_fingerprint_is_deterministic_for_unordered_sets(self):
        first = self.fingerprint(options={"paid_approved_providers": {"image", "video"}})
        second = self.fingerprint(options={"paid_approved_providers": {"video", "image"}})
        self.assertEqual(first, second)

    def test_fingerprint_changes_when_model_changes(self):
        first = self.fingerprint(options={"model": "image-v1"})
        second = self.fingerprint(options={"model": "image-v2"})
        self.assertNotEqual(first, second)

    def test_fingerprint_rejects_unsupported_nested_values(self):
        with self.assertRaises(ValueError):
            self.fingerprint(options={"model": {"secret": "x"}})

    def test_fingerprint_rejects_nonfinite_numbers(self):
        with self.assertRaises(ValueError):
            self.fingerprint(options={"cost": float("nan")})

    def test_invalid_reservation_inputs_fail_closed(self):
        fp = self.fingerprint()
        cases = [
            {"request_id": "bad id", "project_id": "film", "category": "image", "estimated_cost": 1.0, "trace_id": TRACE_A, "fingerprint": fp, "status": "INVALID_REQUEST_ID"},
            {"request_id": "valid", "project_id": "bad project", "category": "image", "estimated_cost": 1.0, "trace_id": TRACE_A, "fingerprint": fp, "status": "INVALID_PROJECT_ID"},
            {"request_id": "valid", "project_id": "film", "category": "", "estimated_cost": 1.0, "trace_id": TRACE_A, "fingerprint": fp, "status": "INVALID_CATEGORY"},
            {"request_id": "valid", "project_id": "film", "category": "image", "estimated_cost": float("inf"), "trace_id": TRACE_A, "fingerprint": fp, "status": "INVALID_ESTIMATED_COST"},
            {"request_id": "valid", "project_id": "film", "category": "image", "estimated_cost": 1.0, "trace_id": "0" * 32, "fingerprint": fp, "status": "INVALID_TRACE_ID"},
            {"request_id": "valid", "project_id": "film", "category": "image", "estimated_cost": 1.0, "trace_id": TRACE_A, "fingerprint": "bad", "status": "INVALID_FINGERPRINT"},
        ]
        for values in cases:
            expected = values.pop("status")
            with self.subTest(expected=expected):
                self.assertEqual(self.ledger.reserve(**values)["status"], expected)

    def test_mark_started_requires_existing_reserved_request(self):
        result = self.ledger.mark_started(request_id="missing", provider_id="image")
        self.assertEqual(result["status"], "REQUEST_NOT_RESERVED")
        self.assertFalse(result["saved"])

    def test_mark_started_validates_provider_id(self):
        self.reserve()
        result = self.ledger.mark_started(request_id="request-1", provider_id="bad provider")
        self.assertEqual(result["status"], "INVALID_PROVIDER_ID")
        self.assertEqual(self.ledger.get("request-1")["request"]["status"], "RESERVED")

    def test_mark_started_is_one_way(self):
        self.reserve()
        self.assertTrue(self.ledger.mark_started(request_id="request-1", provider_id="image")["saved"])
        second = self.ledger.mark_started(request_id="request-1", provider_id="image")
        self.assertEqual(second["status"], "REQUEST_NOT_RESERVED")
        self.assertEqual(self.ledger.get("request-1")["request"]["status"], "STARTED")

    def test_finish_is_idempotent_for_same_terminal_result(self):
        self.reserve()
        self.ledger.mark_started(request_id="request-1", provider_id="image")
        first = self.ledger.finish(
            request_id="request-1", status="COMPLETED",
            result_status="EXECUTION_SUCCEEDED", provider_id="image",
        )
        duplicate = self.ledger.finish(
            request_id="request-1", status="COMPLETED",
            result_status="EXECUTION_SUCCEEDED", provider_id="image",
        )
        self.assertTrue(first["saved"])
        self.assertEqual(duplicate["status"], "ALREADY_FINISHED")
        self.assertTrue(duplicate["duplicate"])

    def test_finish_rejects_conflicting_terminal_result(self):
        self.reserve()
        self.ledger.mark_started(request_id="request-1", provider_id="image")
        self.ledger.finish(
            request_id="request-1", status="FAILED",
            result_status="EXECUTION_FAILED", provider_id="image",
        )
        result = self.ledger.finish(
            request_id="request-1", status="COMPLETED",
            result_status="EXECUTION_SUCCEEDED", provider_id="image",
        )
        self.assertEqual(result["status"], "REQUEST_TERMINAL_CONFLICT")
        self.assertFalse(result["saved"])

    def test_finish_rejects_unknown_and_nonterminal_states(self):
        self.reserve()
        for status in ("RESERVED", "STARTED", "NOT_A_STATE", [], None):
            with self.subTest(status=repr(status)):
                result = self.ledger.finish(
                    request_id="request-1", status=status,
                    result_status="EXECUTION_FAILED",
                )
                self.assertFalse(result["saved"])

    def test_get_validates_id_and_reports_missing(self):
        self.assertEqual(self.ledger.get("bad id")["status"], "INVALID_REQUEST_ID")
        self.assertEqual(self.ledger.get("missing")["status"], "NOT_FOUND")

    def test_capacity_hard_stop_never_evicts_idempotency_records(self):
        tiny = ProviderExecutionLedger(self.path, max_entries=1)
        fp = tiny.fingerprint_request(project_id="film", category="image", estimated_cost=1.0)
        tiny.reserve(
            request_id="first", project_id="film", category="image",
            estimated_cost=1.0, trace_id=TRACE_A, fingerprint=fp,
        )
        second = tiny.reserve(
            request_id="second", project_id="film", category="image",
            estimated_cost=1.0, trace_id=TRACE_A, fingerprint=fp,
        )
        self.assertEqual(second["status"], "LEDGER_CAPACITY_REACHED")
        self.assertEqual(tiny.get("first")["status"], "FOUND")
        self.assertEqual(tiny.get("second")["status"], "NOT_FOUND")

    def test_corrupt_json_is_rejected(self):
        self.path.write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderExecutionLedger(self.path)

    def test_malformed_persisted_record_is_rejected(self):
        self.path.write_text(json.dumps({
            "version": "1.0",
            "requests": {
                "request-1": {
                    "project_id": "film", "category": "image", "estimated_cost": 1.0,
                    "trace_id": "bad", "fingerprint": "f" * 64, "status": "RESERVED",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            },
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            ProviderExecutionLedger(self.path)

    def test_concurrent_threads_allow_only_one_claim_for_same_request(self):
        fingerprint = self.fingerprint()
        def attempt(_):
            return self.ledger.reserve(
                request_id="same", project_id="film", category="image",
                estimated_cost=1.0, trace_id=TRACE_A, fingerprint=fingerprint,
            )
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(attempt, range(40)))
        self.assertEqual(sum(result.get("reserved") is True for result in results), 1)
        self.assertEqual(sum(result["status"] == "REQUEST_IN_PROGRESS" for result in results), 39)

    def test_concurrent_processes_allow_only_one_claim_for_same_request(self):
        args = [(str(self.path), "process-shared-request") for _ in range(20)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_reserve_execution_in_process, args))
        self.assertEqual(sum(result.get("reserved") is True for result in results), 1)
        self.assertEqual(sum(result["status"] == "REQUEST_IN_PROGRESS" for result in results), 19)

    def test_concurrent_processes_preserve_distinct_request_claims(self):
        args = [(str(self.path), f"process-request-{i}") for i in range(20)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_reserve_execution_in_process, args))
        self.assertTrue(all(result.get("reserved") is True for result in results))
        self.assertEqual(len(ProviderExecutionLedger(self.path).data["requests"]), 20)


if __name__ == "__main__":
    unittest.main()
