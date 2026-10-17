import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from projects.provider_store.execution_queue import BoundedExecutionQueue


class BoundedExecutionQueueTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def enqueue(self, queue, request_id, **kwargs):
        defaults = {
            "request_id": request_id,
            "tenant_id": "tenant-a",
            "project_id": "project-a",
            "category": "image",
            "now": self.now,
        }
        defaults.update(kwargs)
        return queue.enqueue(**defaults)

    def test_queue_capacity_and_per_tenant_backpressure(self):
        queue = BoundedExecutionQueue(max_pending=3, max_running=1, max_pending_per_tenant=2)
        self.assertTrue(self.enqueue(queue, "a")["accepted"])
        self.assertTrue(self.enqueue(queue, "b")["accepted"])
        self.assertEqual(self.enqueue(queue, "c")["status"], "TENANT_QUEUE_LIMIT")
        self.assertEqual(self.enqueue(queue, "d", tenant_id="tenant-b")["status"], "QUEUED")
        self.assertEqual(self.enqueue(queue, "e", tenant_id="tenant-c")["status"], "QUEUE_FULL")
        stats = queue.stats()
        self.assertEqual(stats["pending"], 3)
        self.assertEqual(stats["rejected_total"], 2)

    def test_duplicate_request_is_idempotent_but_conflicts_fail_closed(self):
        queue = BoundedExecutionQueue()
        self.assertEqual(self.enqueue(queue, "same")["status"], "QUEUED")
        self.assertEqual(self.enqueue(queue, "same")["status"], "ALREADY_QUEUED")
        conflict = self.enqueue(queue, "same", project_id="other-project")
        self.assertEqual(conflict["status"], "REQUEST_ID_CONFLICT")
        self.assertEqual(queue.stats()["pending"], 1)

    def test_priority_then_fifo_order_and_running_capacity(self):
        queue = BoundedExecutionQueue(max_pending=10, max_running=1)
        self.enqueue(queue, "low", priority=1)
        self.enqueue(queue, "high-first", priority=10)
        self.enqueue(queue, "high-second", priority=10)
        first = queue.claim_next(worker_id="worker-1", now=self.now)
        self.assertEqual(first["job"]["request_id"], "high-first")
        self.assertEqual(queue.claim_next(worker_id="worker-2", now=self.now)["status"], "WORKER_CAPACITY_FULL")
        self.assertEqual(queue.complete(
            request_id="high-first", worker_id="worker-1", status="SUCCEEDED", now=self.now
        )["status"], "COMPLETED")
        self.assertEqual(queue.claim_next(worker_id="worker-2", now=self.now)["job"]["request_id"], "high-second")

    def test_tenant_round_robin_prevents_busy_tenant_monopolizing_workers(self):
        queue = BoundedExecutionQueue(max_pending=10, max_running=1, max_pending_per_tenant=10)
        self.enqueue(queue, "a-high-1", tenant_id="tenant-a", priority=100)
        self.enqueue(queue, "a-high-2", tenant_id="tenant-a", priority=100)
        self.enqueue(queue, "a-high-3", tenant_id="tenant-a", priority=100)
        self.enqueue(queue, "b-low", tenant_id="tenant-b", priority=0)
        first = queue.claim_next(worker_id="worker-1", now=self.now)
        self.assertEqual(first["job"]["request_id"], "a-high-1")
        queue.complete(request_id="a-high-1", worker_id="worker-1", status="SUCCEEDED", now=self.now)
        second = queue.claim_next(worker_id="worker-1", now=self.now)
        self.assertEqual(second["job"]["request_id"], "b-low")
        queue.complete(request_id="b-low", worker_id="worker-1", status="SUCCEEDED", now=self.now)
        third = queue.claim_next(worker_id="worker-1", now=self.now)
        self.assertEqual(third["job"]["request_id"], "a-high-2")

    def test_wrong_worker_cannot_complete_running_job(self):
        queue = BoundedExecutionQueue()
        self.enqueue(queue, "job")
        queue.claim_next(worker_id="owner", now=self.now)
        result = queue.complete(request_id="job", worker_id="attacker", status="SUCCEEDED", now=self.now)
        self.assertEqual(result["status"], "WORKER_OWNERSHIP_MISMATCH")
        self.assertEqual(queue.stats()["running"], 1)

    def test_cancel_queued_job_removes_it_and_preserves_idempotency(self):
        queue = BoundedExecutionQueue()
        self.enqueue(queue, "job")
        self.assertEqual(queue.cancel(request_id="job", tenant_id="tenant-b", now=self.now)["status"], "TENANT_SCOPE_MISMATCH")
        result = queue.cancel(request_id="job", tenant_id="tenant-a", now=self.now)
        self.assertTrue(result["cancelled"])
        self.assertEqual(queue.stats()["pending"], 0)
        self.assertEqual(self.enqueue(queue, "job")["status"], "ALREADY_TERMINAL")

    def test_cancel_running_job_does_not_release_capacity_until_worker_finishes(self):
        queue = BoundedExecutionQueue(max_running=1)
        self.enqueue(queue, "job")
        queue.claim_next(worker_id="worker", now=self.now)
        result = queue.cancel(request_id="job", tenant_id="tenant-a", now=self.now)
        self.assertEqual(result["status"], "CANCEL_REQUESTED")
        self.assertEqual(queue.stats()["running"], 1)
        self.assertEqual(queue.claim_next(worker_id="other", now=self.now)["status"], "QUEUE_EMPTY")
        completed = queue.complete(request_id="job", worker_id="worker", status="CANCELLED", now=self.now)
        self.assertTrue(completed["completed"])
        self.assertEqual(queue.stats()["running"], 0)

    def test_running_cancellation_is_visible_only_to_owning_worker(self):
        queue = BoundedExecutionQueue(max_running=1)
        self.enqueue(queue, "job")
        queue.claim_next(worker_id="worker-a", now=self.now)
        self.assertEqual(
            queue.cancel(request_id="job", tenant_id="tenant-a", now=self.now)["status"],
            "CANCEL_REQUESTED",
        )
        wrong_worker = queue.get_job(request_id="job", tenant_id="tenant-a", worker_id="worker-b")
        self.assertEqual(wrong_worker["status"], "WORKER_OWNERSHIP_MISMATCH")
        wrong_tenant = queue.get_job(request_id="job", tenant_id="tenant-b", worker_id="worker-a")
        self.assertEqual(wrong_tenant["status"], "NOT_FOUND")
        owner = queue.get_job(request_id="job", tenant_id="tenant-a", worker_id="worker-a")
        self.assertEqual(owner["status"], "FOUND")
        self.assertTrue(owner["job"]["cancel_requested"])
        self.assertNotIn("fingerprint", repr(owner["job"]))
        queue.complete(request_id="job", worker_id="worker-a", status="CANCELLED", now=self.now)
        terminal = queue.get_job(request_id="job", tenant_id="tenant-a")
        self.assertEqual(terminal["status"], "TERMINAL")
        self.assertEqual(terminal["job"]["status"], "CANCELLED")

    def test_expired_queued_jobs_are_removed_but_running_deadlines_hold_capacity(self):
        queue = BoundedExecutionQueue(max_running=1)
        self.enqueue(queue, "queued-expired", priority=1, deadline_at=(self.now + timedelta(seconds=5)).isoformat())
        self.enqueue(queue, "running-expired", priority=10, deadline_at=(self.now + timedelta(seconds=5)).isoformat())
        self.enqueue(queue, "pending-next", priority=0)
        claimed = queue.claim_next(worker_id="worker", now=self.now)
        running_id = claimed["job"]["request_id"]
        result = queue.reap_expired(now=self.now + timedelta(seconds=6))
        self.assertEqual(result["pending_expired"], 1)
        self.assertEqual(result["running_deadlines_flagged"], 1)
        self.assertEqual(queue.stats()["running"], 1)
        self.assertEqual(queue.claim_next(worker_id="next", now=self.now)["status"], "WORKER_CAPACITY_FULL")
        self.assertTrue(queue.complete(
            request_id=running_id, worker_id="worker", status="FAILED",
            now=self.now + timedelta(seconds=7),
        )["completed"])

    def test_deadline_already_expired_and_invalid_deadlines_are_rejected(self):
        queue = BoundedExecutionQueue()
        self.assertEqual(self.enqueue(
            queue, "past", deadline_at=(self.now - timedelta(seconds=1)).isoformat()
        )["status"], "DEADLINE_ALREADY_EXPIRED")
        self.assertEqual(self.enqueue(queue, "bad", deadline_at="not-a-date")["status"], "INVALID_DEADLINE")
        self.assertEqual(self.enqueue(queue, "naive", deadline_at="2026-01-01T00:00:00")["status"], "INVALID_DEADLINE")

    def test_metadata_and_constructor_limits_are_validated(self):
        queue = BoundedExecutionQueue()
        self.assertEqual(self.enqueue(queue, "bad", priority=True)["status"], "INVALID_PRIORITY")
        self.assertEqual(self.enqueue(queue, "bad\nrequest")["status"], "INVALID_REQUEST_METADATA")
        with self.assertRaises(ValueError):
            BoundedExecutionQueue(max_pending=0)
        with self.assertRaises(ValueError):
            BoundedExecutionQueue(max_running=True)

    def test_concurrent_enqueue_never_exceeds_pending_capacity(self):
        queue = BoundedExecutionQueue(max_pending=40, max_running=1, max_pending_per_tenant=40)
        def put(index):
            return self.enqueue(queue, f"job-{index}")["accepted"]
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(put, range(100)))
        self.assertEqual(sum(results), 40)
        self.assertEqual(queue.stats()["pending"], 40)
        self.assertEqual(queue.stats()["accepted_total"], 40)
        self.assertEqual(queue.stats()["rejected_total"], 60)

    def test_stats_never_include_request_payloads_or_identifiers(self):
        queue = BoundedExecutionQueue()
        self.enqueue(queue, "private-request-id")
        stats = queue.stats()
        self.assertNotIn("private-request-id", repr(stats))
        self.assertEqual(set(stats), {
            "status", "pending", "running", "max_pending", "max_running",
            "max_pending_per_tenant", "accepted_total", "rejected_total",
        })


if __name__ == "__main__":
    unittest.main()
