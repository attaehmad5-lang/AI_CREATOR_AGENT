import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.paid_approval_manager import PaidUsageApprovalManager


class PaidUsageApprovalManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = [datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)]
        self.manager = PaidUsageApprovalManager(
            self.root / "approvals.json", clock=lambda: self.now[0]
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def request(self, **overrides):
        values = {
            "project_id": "film", "provider_id": "paid-provider",
            "request_id": "job-1", "estimated_cost": 2.0,
            "unit": "USD", "requested_by": "user-1",
            "reason": "Approved documentary generation budget",
        }
        values.update(overrides)
        return self.manager.request_approval(**values)

    def approved(self, **overrides):
        pending = self.request(**overrides)
        self.assertEqual(pending["status"], "APPROVAL_PENDING")
        result = self.manager.approve(pending["approval_id"], approved_by="admin-1")
        self.assertEqual(result["status"], "APPROVED")
        return pending["approval_id"]

    def test_request_is_pending_until_an_explicit_approver_acts(self):
        pending = self.request()
        self.assertEqual(pending["status"], "APPROVAL_PENDING")
        result = self.manager.authorize(
            pending["approval_id"], project_id="film", provider_id="paid-provider",
            request_id="job-1", estimated_cost=2.0, unit="USD",
        )
        self.assertFalse(result["approved"])
        self.assertEqual(result["status"], "PAID_APPROVAL_REQUIRED")

    def test_approval_is_scoped_persisted_and_reloaded(self):
        approval_id = self.approved()
        reloaded = PaidUsageApprovalManager(self.root / "approvals.json", clock=lambda: self.now[0])
        result = reloaded.authorize(
            approval_id, project_id="film", provider_id="paid-provider",
            request_id="job-1", estimated_cost=2.0, unit="USD",
        )
        self.assertTrue(result["approved"])
        self.assertEqual(result["status"], "PAID_APPROVAL_VALID")
        self.assertEqual(result["approved_by"], "admin-1")
        self.assertNotIn(str(self.root), repr(result))

    def test_duplicate_request_does_not_create_second_approval(self):
        first = self.request()
        second = self.request()
        self.assertEqual(first["status"], "APPROVAL_PENDING")
        self.assertEqual(second["status"], "APPROVAL_ALREADY_EXISTS")
        self.assertEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(len(self.manager.list_approvals()["approvals"]), 1)

    def test_fifty_invalid_approval_scope_cases_fail_closed(self):
        cases = []
        for field in ("project_id", "provider_id", "request_id", "requested_by"):
            cases.extend((field, value) for value in ("", None, 5, "bad id", "secret=do-not-store"))
        for index, (field, value) in enumerate(cases):
            with self.subTest(case=index, field=field):
                overrides = {field: value}
                if field != "request_id":
                    overrides["request_id"] = f"bad-{index}"
                result = self.request(**overrides)
                self.assertFalse(result["approval_created"])
                self.assertEqual(result["status"], "INVALID_APPROVAL_SCOPE")
        for index in range(30):
            with self.subTest(case=20 + index):
                result = self.request(reason="secret=hidden" if index % 2 == 0 else "x" * 501)
                self.assertFalse(result["approval_created"])
                self.assertEqual(result["status"], "INVALID_APPROVAL_REASON")

    def test_fifty_scope_unit_and_amount_mismatches_never_authorize(self):
        for index in range(50):
            with self.subTest(case=index):
                approval_id = self.approved(
                    request_id=f"scope-job-{index}",
                    estimated_cost=1.0,
                )
                mismatch = index % 5
                values = {
                    "project_id": "film", "provider_id": "paid-provider",
                    "request_id": f"scope-job-{index}", "estimated_cost": 1.0, "unit": "USD",
                }
                expected = "APPROVAL_SCOPE_MISMATCH"
                if mismatch == 0:
                    values["project_id"] = "other-project"
                elif mismatch == 1:
                    values["provider_id"] = "other-provider"
                elif mismatch == 2:
                    values["request_id"] = "other-request"
                elif mismatch == 3:
                    values["unit"] = "credits"
                else:
                    values["estimated_cost"] = 1.01
                    expected = "APPROVAL_AMOUNT_EXCEEDED"
                result = self.manager.authorize(approval_id, **values)
                self.assertFalse(result["approved"])
                self.assertEqual(result["status"], expected)

    def test_expiry_revoke_and_invalid_approval_limits_fail_closed(self):
        approval_id = self.request()["approval_id"]
        self.assertEqual(
            self.manager.approve(approval_id, approved_by="admin-1", max_amount=1.0)["status"],
            "APPROVAL_LIMIT_BELOW_REQUEST",
        )
        self.assertEqual(
            self.manager.approve(approval_id, approved_by="admin-1", expires_in_seconds=0)["status"],
            "INVALID_APPROVAL_EXPIRY",
        )
        self.manager.approve(approval_id, approved_by="admin-1", expires_in_seconds=60)
        self.now[0] += timedelta(seconds=61)
        self.assertEqual(
            self.manager.authorize(
                approval_id, project_id="film", provider_id="paid-provider",
                request_id="job-1", estimated_cost=2.0, unit="USD",
            )["status"],
            "APPROVAL_EXPIRED",
        )
        second = self.request(request_id="job-2")
        self.manager.approve(second["approval_id"], approved_by="admin-1")
        self.assertTrue(self.manager.revoke(second["approval_id"], revoked_by="admin-1")["revoked"])
        self.assertEqual(
            self.manager.authorize(
                second["approval_id"], project_id="film", provider_id="paid-provider",
                request_id="job-2", estimated_cost=2.0, unit="USD",
            )["status"],
            "PAID_APPROVAL_REQUIRED",
        )

    def test_concurrent_approval_attempts_only_approve_once(self):
        pending = self.request()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda index: self.manager.approve(pending["approval_id"], approved_by=f"admin-{index}"),
                range(8),
            ))
        self.assertEqual(sum(result["approved"] is True for result in results), 1)
        self.assertEqual(len(self.manager.list_approvals()["approvals"]), 1)


if __name__ == "__main__":
    unittest.main()
