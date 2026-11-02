"""Tenant-scoped provider job persistence and service authorization tests."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.provider_store.provider_job_manager import ProviderJobManager
from projects.provider_store.provider_service import ProviderService
from projects.provider_store.provider_tenant_isolation import ProviderTenantIsolationManager


class FakeConnectionManager:
    def verify_connectivity(self, *_args, **_kwargs):
        return {"connected": False}


class AllowAuthorizationManager:
    def authorize(self, principal_id, operation):
        if not isinstance(principal_id, str) or not principal_id:
            return {"allowed": False, "status": "INVALID_PRINCIPAL"}
        return {"allowed": True, "principal_id": principal_id, "operation": operation}


class ProviderJobTenantScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        self.jobs_path = self.root / "jobs.json"
        self.manager = ProviderJobManager(
            self.jobs_path, clock=lambda: self.now,
            max_queue_size=20, max_concurrent=2,
        )

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, *, job_id="tenant-job", tenant_id="tenant-a", project_id="project-a"):
        return self.manager.submit(
            job_id=job_id, tenant_id=tenant_id,
            project_id=project_id, provider_id="image",
            payload_ref=f"payload:{job_id}",
        )

    def test_job_record_persists_tenant_and_only_matching_tenant_can_poll_or_cancel(self):
        self.assertEqual(self.submit()["status"], "JOB_QUEUED")
        persisted = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["jobs"]["tenant-job"]["tenant_id"], "tenant-a")
        self.assertEqual(
            self.manager.poll(job_id="tenant-job", tenant_id="tenant-a")["status"],
            "JOB_STATUS",
        )
        for tenant_id in (None, "tenant-b"):
            with self.subTest(tenant_id=tenant_id):
                result = self.manager.poll(job_id="tenant-job", tenant_id=tenant_id)
                self.assertEqual(result["status"], "TENANT_ISOLATION_DENIED")
                self.assertIsNone(result["job"])
                cancel = self.manager.cancel(job_id="tenant-job", tenant_id=tenant_id)
                self.assertEqual(cancel["status"], "TENANT_ISOLATION_DENIED")
                self.assertFalse(cancel["saved"])
        self.assertEqual(
            self.manager.poll(job_id="tenant-job", tenant_id="tenant-a")["job"]["status"],
            "QUEUED",
        )
        self.assertTrue(self.manager.cancel(job_id="tenant-job", tenant_id="tenant-a")["saved"])

    def test_job_idempotency_cannot_be_reused_across_tenants(self):
        self.submit(job_id="shared-job", tenant_id="tenant-a")
        result = self.manager.submit(
            job_id="shared-job", tenant_id="tenant-b",
            project_id="project-b", provider_id="image",
            payload_ref="payload:shared-job",
        )
        self.assertEqual(result["status"], "TENANT_ISOLATION_DENIED")
        self.assertNotIn("job", result)

    def test_legacy_tenantless_jobs_remain_backward_compatible_but_are_not_claimed_by_tenants(self):
        result = self.manager.submit(
            job_id="legacy-job", project_id="legacy-project",
            provider_id="image", payload_ref="payload:legacy",
        )
        self.assertEqual(result["status"], "JOB_QUEUED")
        self.assertEqual(self.manager.poll(job_id="legacy-job")["status"], "JOB_STATUS")
        tenant_poll = self.manager.poll(job_id="legacy-job", tenant_id="tenant-a")
        self.assertEqual(tenant_poll["status"], "TENANT_ISOLATION_DENIED")
        self.assertIsNone(tenant_poll["job"])

    def test_invalid_tenant_values_and_corrupt_persisted_tenant_are_rejected(self):
        invalid = self.manager.submit(
            job_id="bad-job", tenant_id="../tenant", project_id="project",
            provider_id="image", payload_ref="payload:bad",
        )
        self.assertEqual(invalid["status"], "INVALID_TENANT_ID")
        self.submit()
        data = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        data["jobs"]["tenant-job"]["tenant_id"] = ["malformed"]
        self.jobs_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid provider job record"):
            ProviderJobManager(self.jobs_path)

    def test_claimed_job_retains_tenant_context_for_worker_execution(self):
        self.submit()
        claimed = self.manager.claim_next(worker_id="worker-1")
        self.assertEqual(claimed["status"], "JOB_CLAIMED")
        self.assertEqual(claimed["job"]["tenant_id"], "tenant-a")

    def test_manager_restart_retains_tenant_scope(self):
        self.submit()
        restarted = ProviderJobManager(self.jobs_path, clock=lambda: self.now)
        self.assertEqual(
            restarted.poll(job_id="tenant-job", tenant_id="tenant-a")["status"],
            "JOB_STATUS",
        )
        self.assertEqual(
            restarted.poll(job_id="tenant-job", tenant_id="tenant-b")["status"],
            "TENANT_ISOLATION_DENIED",
        )


class ProviderServiceTenantJobScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tenant_manager = ProviderTenantIsolationManager(self.root / "tenants.json")
        for tenant_id, principal_id, project_id in (
            ("tenant-a", "user-a", "project-a"),
            ("tenant-b", "user-b", "project-b"),
        ):
            self.tenant_manager.register_tenant(tenant_id)
            self.tenant_manager.bind_principal(tenant_id, principal_id)
            self.tenant_manager.bind_project(tenant_id, project_id)
        self.jobs = ProviderJobManager(
            self.root / "service-jobs.json", max_queue_size=10,
        )
        self.service = ProviderService(
            FakeConnectionManager(),
            authorization_manager=AllowAuthorizationManager(),
            tenant_isolation_manager=self.tenant_manager,
            job_manager=self.jobs,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_service_job_submission_requires_tenant_and_project_membership(self):
        missing = self.service.submit_job(
            job_id="missing-tenant", project_id="project-a", provider_id="image",
            payload_ref="payload:missing", principal_id="user-a",
        )
        self.assertEqual(missing["status"], "TENANT_CONTEXT_REQUIRED")
        denied = self.service.submit_job(
            job_id="cross-tenant", project_id="project-a", provider_id="image",
            payload_ref="payload:cross", principal_id="user-b", tenant_id="tenant-b",
        )
        self.assertEqual(denied["status"], "TENANT_ISOLATION_DENIED")
        self.assertEqual(len(self.jobs.data["jobs"]), 0)
        allowed = self.service.submit_job(
            job_id="valid-tenant", project_id="project-a", provider_id="image",
            payload_ref="payload:valid", principal_id="user-a", tenant_id="tenant-a",
        )
        self.assertEqual(allowed["status"], "JOB_QUEUED")
        self.assertEqual(allowed["job"]["tenant_id"], "tenant-a")

    def test_service_poll_and_cancel_never_return_or_mutate_cross_tenant_job(self):
        self.service.submit_job(
            job_id="tenant-a-job", project_id="project-a", provider_id="image",
            payload_ref="payload:a", principal_id="user-a", tenant_id="tenant-a",
        )
        missing_context = self.service.poll_job(
            job_id="tenant-a-job", principal_id="user-a",
        )
        self.assertEqual(missing_context["status"], "TENANT_CONTEXT_REQUIRED")
        missing_cancel_context = self.service.cancel_job(
            job_id="tenant-a-job", principal_id="user-a",
        )
        self.assertEqual(missing_cancel_context["status"], "TENANT_CONTEXT_REQUIRED")
        hidden = self.service.poll_job(
            job_id="tenant-a-job", principal_id="user-b", tenant_id="tenant-b",
        )
        self.assertEqual(hidden["status"], "TENANT_ISOLATION_DENIED")
        self.assertIsNone(hidden["job"])
        cancelled = self.service.cancel_job(
            job_id="tenant-a-job", principal_id="user-b", tenant_id="tenant-b",
        )
        self.assertEqual(cancelled["status"], "TENANT_ISOLATION_DENIED")
        self.assertFalse(cancelled["saved"])
        self.assertEqual(
            self.service.poll_job(
                job_id="tenant-a-job", principal_id="user-a", tenant_id="tenant-a",
            )["job"]["status"],
            "QUEUED",
        )

    def test_service_denies_same_tenant_nonmember_before_returning_job(self):
        self.service.submit_job(
            job_id="tenant-a-job", project_id="project-a", provider_id="image",
            payload_ref="payload:a", principal_id="user-a", tenant_id="tenant-a",
        )
        result = self.service.poll_job(
            job_id="tenant-a-job", principal_id="other-user", tenant_id="tenant-a",
        )
        self.assertEqual(result["status"], "TENANT_ISOLATION_DENIED")
        self.assertIsNone(result["job"])


if __name__ == "__main__":
    unittest.main()
