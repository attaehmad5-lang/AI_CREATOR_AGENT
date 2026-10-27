"""Regression tests for AdvancedStoreBridge's canonical execution hand-off."""
import unittest

from projects.provider_store.advanced.advanced_store_bridge import AdvancedStoreBridge


class ExecutionDelegationTests(unittest.TestCase):
    def setUp(self):
        self.bridge = AdvancedStoreBridge.__new__(AdvancedStoreBridge)

    def test_delegates_to_provider_service_without_rewriting_request(self):
        class Service:
            def execute_routed_request(self, **kwargs):
                self.request = kwargs
                return {"status": "COMPLETED", "executed": True}

        service = Service()
        request = {"category": "image", "project_id": "film", "request_id": "req-1",
                   "estimated_cost": 0.05, "adapter": lambda **kw: {"started": True},
                   "modality": "image"}
        result = self.bridge.execute_via_service(service, **request)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(service.request, request)

    def test_missing_service_fails_closed(self):
        self.assertEqual(
            self.bridge.execute_via_service(object(), request_id="req-2"),
            {"status": "PROVIDER_SERVICE_UNAVAILABLE", "executed": False},
        )

    def test_unexpected_exception_is_treated_as_ambiguous_paid_outcome(self):
        class BrokenService:
            def execute_routed_request(self, **kwargs):
                raise RuntimeError("private details")

        result = self.bridge.execute_via_service(BrokenService(), request_id="req-3")
        self.assertEqual(result["status"], "EXECUTION_OUTCOME_UNKNOWN")
        self.assertEqual(result["reservation_state"], "UNKNOWN")
        self.assertTrue(result["manual_reconciliation_required"])
        self.assertEqual(result["request_id"], "req-3")
        self.assertNotIn("private details", repr(result))


if __name__ == "__main__":
    unittest.main()
