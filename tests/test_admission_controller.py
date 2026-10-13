import math
import unittest
from concurrent.futures import ThreadPoolExecutor

from projects.provider_store.admission_controller import ProviderAdmissionController


class ProviderAdmissionControllerTests(unittest.TestCase):
    def test_global_backpressure_limit_is_enforced_concurrently(self):
        controller = ProviderAdmissionController(max_active=4, max_active_per_project=4)
        def acquire(i):
            return controller.acquire(request_id=f"req-{i}", project_id=f"project-{i}")
        with ThreadPoolExecutor(max_workers=24) as pool:
            results = list(pool.map(acquire, range(24)))
        self.assertEqual(sum(result["acquired"] for result in results), 4)
        self.assertEqual(
            sum(result["status"] == "BACKPRESSURE_GLOBAL_LIMIT" for result in results), 20
        )
        self.assertEqual(controller.snapshot()["active"], 4)

    def test_per_project_backpressure_is_enforced(self):
        controller = ProviderAdmissionController(max_active=10, max_active_per_project=2)
        results = [
            controller.acquire(request_id=f"project-job-{i}", project_id="film")
            for i in range(5)
        ]
        self.assertEqual(sum(result["acquired"] for result in results), 2)
        self.assertEqual(
            sum(result["status"] == "BACKPRESSURE_PROJECT_LIMIT" for result in results), 3
        )

    def test_duplicate_requests_and_release_are_idempotent(self):
        controller = ProviderAdmissionController()
        first = controller.acquire(request_id="request-1", project_id="film")
        self.assertTrue(first["acquired"])
        self.assertEqual(
            controller.acquire(request_id="request-1", project_id="film")["status"],
            "DUPLICATE_ACTIVE_REQUEST",
        )
        self.assertTrue(controller.release(request_id="request-1")["released"])
        self.assertEqual(
            controller.release(request_id="request-1")["status"], "ALREADY_RELEASED"
        )
        self.assertEqual(
            controller.acquire(request_id="request-1", project_id="film")["status"],
            "DUPLICATE_COMPLETED_REQUEST",
        )

    def test_shutdown_and_reopen_do_not_discard_active_leases(self):
        controller = ProviderAdmissionController()
        controller.acquire(request_id="request-1", project_id="film")
        self.assertFalse(controller.reopen()["reopened"])
        controller.shutdown()
        self.assertEqual(
            controller.acquire(request_id="request-2", project_id="film")["status"],
            "PROVIDER_ADMISSION_CLOSED",
        )
        controller.release(request_id="request-1")
        self.assertTrue(controller.reopen()["reopened"])
        self.assertTrue(controller.acquire(request_id="request-2", project_id="film")["acquired"])

    def test_invalid_limits_and_ids_fail_closed(self):
        with self.assertRaises(ValueError):
            ProviderAdmissionController(max_active=0)
        with self.assertRaises(ValueError):
            ProviderAdmissionController(max_active=2, max_active_per_project=3)
        controller = ProviderAdmissionController()
        self.assertEqual(
            controller.acquire(request_id="bad id", project_id="film")["status"],
            "INVALID_REQUEST_ID",
        )
        self.assertEqual(
            controller.acquire(request_id="valid", project_id="bad id")["status"],
            "INVALID_PROJECT_ID",
        )

    def test_invalid_clock_value_is_rejected(self):
        for value in (float("nan"), float("inf"), True, "bad"):
            controller = ProviderAdmissionController(clock=lambda value=value: value)
            self.assertEqual(
                controller.acquire(request_id="request-1", project_id="film")["status"],
                "INVALID_ADMISSION_CLOCK",
            )


if __name__ == "__main__":
    unittest.main()
