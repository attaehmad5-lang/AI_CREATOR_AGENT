import copy
import unittest
from types import SimpleNamespace

from projects.provider_store.state_reconciliation_manager import (
    ProviderStoreReconciliationManager,
)


class ProviderStoreReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.jobs = {
            "job-1": {
                "job_id": "job-1", "provider_id": "image",
                "status": "SUCCEEDED",
            },
        }
        self.quota = {
            "job-1": {"provider_id": "image", "status": "COMMITTED"},
        }
        self.credit = {
            "reservations": {},
            "usage": {
                "job-1": {"provider_id": "image", "amount": 1, "unit": "credits"},
            },
        }
        self.cost = {
            "projects": {
                "film": {
                    "reservations": {},
                    "requests": {
                        "job-1": {"provider_id": "image", "estimated_cost": 0.04},
                    },
                },
            },
        }
        self.job_manager = SimpleNamespace(data={"jobs": self.jobs})
        self.quota_manager = SimpleNamespace(data={"requests": self.quota})
        self.credit_manager = SimpleNamespace(data=self.credit)
        self.cost_policy = SimpleNamespace(ledger=self.cost)
        self.manager = ProviderStoreReconciliationManager(
            job_manager=self.job_manager,
            quota_manager=self.quota_manager,
            credit_manager=self.credit_manager,
            cost_policy=self.cost_policy,
        )

    def test_consistent_cross_manager_request_is_reconciled(self):
        result = self.manager.inspect()
        self.assertEqual(result["status"], "RECONCILED")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["finding_count"], 0)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["automatic_releases"], 0)

    def test_provider_mismatch_is_reported_across_ledgers(self):
        self.quota["job-1"]["provider_id"] = "video"
        result = self.manager.inspect()
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(result["findings"][0]["code"], "REQUEST_PROVIDER_MISMATCH")
        self.assertEqual(result["findings"][0]["provider_ids"], ["image", "video"])

    def test_terminal_job_with_live_reservations_requires_manual_review(self):
        self.quota["job-1"]["status"] = "RESERVED"
        self.credit["reservations"]["job-1"] = {
            "provider_id": "image", "status": "RESERVED",
            "amount": 2, "unit": "credits",
        }
        self.cost["projects"]["film"]["reservations"]["job-1"] = {
            "provider_id": "image", "status": "RESERVED",
            "estimated_cost": 0.04,
        }
        before = copy.deepcopy((self.jobs, self.quota, self.credit, self.cost))
        result = self.manager.inspect()
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        finding = next(
            item for item in result["findings"]
            if item["code"] == "TERMINAL_JOB_HAS_ACTIVE_RESERVATION"
        )
        self.assertEqual(
            set(finding["reservation_sources"]),
            {"quota", "credit_reservation", "cost_reservation"},
        )
        self.assertEqual(finding["action"], "MANUAL_BILLING_AND_USAGE_REVIEW")
        self.assertEqual((self.jobs, self.quota, self.credit, self.cost), before)

    def test_orphan_synchronous_request_is_inventory_not_false_positive(self):
        self.quota["sync-request"] = {"provider_id": "image", "status": "RESERVED"}
        result = self.manager.inspect()
        self.assertEqual(result["status"], "RECONCILED")
        sync = next(item for item in result["requests"] if item["request_id"] == "sync-request")
        self.assertIsNone(sync["job_status"])

    def test_constructor_rejects_unvalidated_manager_shapes(self):
        with self.assertRaises(ValueError):
            ProviderStoreReconciliationManager(
                job_manager=SimpleNamespace(data={"jobs": []}),
                quota_manager=self.quota_manager,
                credit_manager=self.credit_manager,
                cost_policy=self.cost_policy,
            )


if __name__ == "__main__":
    unittest.main()
