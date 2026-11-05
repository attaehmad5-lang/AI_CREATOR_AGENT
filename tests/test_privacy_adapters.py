"""Deployment adapter and privacy-retention regression tests."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from projects.provider_store.privacy_adapters import (
    HTTPSPrivacyProviderAdapter,
    PersistentPrivacyAdapter,
    PrivacyAdapterConfigurationError,
    PrivacyRetentionJob,
    SQLitePrivacyDataAdapter,
)


class Delegate:
    def __init__(self, result=None):
        self.result = result or {"confirmed": True, "status": "DELETED", "deleted_count": 1}
        self.calls = []

    def delete_subject_data(self, **kwargs):
        self.calls.append(("delete", dict(kwargs)))
        return dict(self.result)

    def purge_expired_data(self, **kwargs):
        self.calls.append(("purge", dict(kwargs)))
        result = dict(self.result)
        if result["status"] == "DELETED":
            result["status"] = "PURGED"
        return result


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body, self.closed = status, body, False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self.body[:size]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class PrivacyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def wrapper(self, delegate, **kwargs):
        options = {
            "ledger_path": self.root / "dedupe.sqlite",
            "adapter_id": "db-one",
            "digest_key_provider": lambda context: b"d" * 32,
        }
        options.update(kwargs)
        return PersistentPrivacyAdapter(delegate, **options)

    def delete_args(self, suffix="01"):
        return {
            "subject_ref": "subject-ref-" + suffix,
            "project_id": "project-id-01",
            "tenant_id": "tenant-id-01",
            "request_id": "request-id-" + suffix,
        }

    def test_durable_replay_after_recreation_does_not_repeat_delete(self):
        delegate = Delegate()
        args = self.delete_args()
        self.assertTrue(self.wrapper(delegate).delete_subject_data(**args)["confirmed"])
        replay = self.wrapper(delegate).delete_subject_data(**args)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(delegate.calls), 1)

    def test_request_id_reuse_for_different_scope_fails_closed(self):
        delegate = Delegate()
        wrapper = self.wrapper(delegate)
        args = self.delete_args("02")
        wrapper.delete_subject_data(**args)
        args["project_id"] = "project-id-02"
        result = wrapper.delete_subject_data(**args)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(len(delegate.calls), 1)

    def test_concurrent_workers_claim_same_request_only_once_during_lease(self):
        started, release = threading.Event(), threading.Event()

        class BlockingDelegate(Delegate):
            def delete_subject_data(inner, **kwargs):
                inner.calls.append(("delete", dict(kwargs)))
                started.set()
                release.wait(3)
                return {"confirmed": True, "status": "DELETED", "deleted_count": 1}

        delegate = BlockingDelegate()
        first, second = self.wrapper(delegate), self.wrapper(delegate)
        args = self.delete_args("03")
        output = []
        worker = threading.Thread(target=lambda: output.append(first.delete_subject_data(**args)))
        worker.start()
        self.assertTrue(started.wait(2))
        concurrent = second.delete_subject_data(**args)
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(concurrent["status"], "PENDING")
        self.assertTrue(output[0]["confirmed"])
        self.assertEqual(len(delegate.calls), 1)

    def test_expired_lease_uses_fencing_attempt_to_reject_stale_worker_write(self):
        now = [100.0]
        first_started, release_first = threading.Event(), threading.Event()

        class FencedDelegate(Delegate):
            def delete_subject_data(inner, **kwargs):
                inner.calls.append(("delete", dict(kwargs)))
                if len(inner.calls) == 1:
                    first_started.set()
                    release_first.wait(3)
                return {"confirmed": True, "status": "DELETED", "deleted_count": 1}

        delegate = FencedDelegate()
        wrapper = self.wrapper(delegate, lease_seconds=5, clock=lambda: now[0])
        args = self.delete_args("04")
        first_result = []
        worker = threading.Thread(target=lambda: first_result.append(wrapper.delete_subject_data(**args)))
        worker.start()
        self.assertTrue(first_started.wait(2))
        now[0] += 6
        second_result = wrapper.delete_subject_data(**args)
        release_first.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertTrue(second_result["confirmed"])
        self.assertEqual(first_result[0]["status"], "PENDING")
        replay = wrapper.delete_subject_data(**args)
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(delegate.calls), 2)

    def test_pending_response_must_not_claim_deleted_rows(self):
        delegate = Delegate({"confirmed": False, "status": "PENDING", "deleted_count": 1})
        result = self.wrapper(delegate).delete_subject_data(**self.delete_args("05"))
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["confirmed"])

    def test_hmac_key_must_be_32_bytes_and_rotation_fails_closed(self):
        with self.assertRaises(PrivacyAdapterConfigurationError):
            self.wrapper(Delegate(), digest_key_provider=lambda context: b"short")
        key = [b"d" * 32]
        delegate = Delegate()
        wrapper = self.wrapper(delegate, digest_key_provider=lambda context: key[0])
        key[0] = b"x" * 32
        result = wrapper.delete_subject_data(**self.delete_args("06"))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(delegate.calls, [])

    def test_ledger_contains_no_raw_scope_or_request_identifiers(self):
        delegate = Delegate()
        wrapper = self.wrapper(delegate)
        wrapper.delete_subject_data(**self.delete_args("07"))
        raw = (self.root / "dedupe.sqlite").read_bytes()
        for marker in (b"subject-ref-07", b"project-id-01", b"tenant-id-01", b"request-id-07"):
            self.assertNotIn(marker, raw)

    def test_prune_only_removes_terminal_ledger_rows(self):
        now = [100.0]
        delegate = Delegate()
        wrapper = self.wrapper(delegate, clock=lambda: now[0])
        wrapper.delete_subject_data(**self.delete_args("08"))
        result = wrapper.prune_completed_before(datetime.fromtimestamp(101, timezone.utc))
        self.assertTrue(result["pruned"])
        self.assertEqual(result["pruned_count"], 1)
        self.assertTrue(wrapper.delete_subject_data(**self.delete_args("08"))["confirmed"])
        self.assertEqual(len(delegate.calls), 2)

    def test_aware_cutoff_required_and_normalized_to_utc_for_idempotency(self):
        delegate = Delegate()
        wrapper = self.wrapper(delegate)
        args = {
            "project_id": "project-id-01",
            "tenant_id": "tenant-id-01",
            "request_id": "request-id-09",
        }
        naive = wrapper.purge_expired_data(cutoff=datetime(2026, 11, 1), **args)
        self.assertEqual(naive["status"], "FAILED")
        cutoff = datetime(2026, 11, 1, tzinfo=timezone.utc)
        first = wrapper.purge_expired_data(cutoff=cutoff, **args)
        second = wrapper.purge_expired_data(
            cutoff=cutoff.astimezone(timezone(timedelta(hours=3))), **args
        )
        self.assertTrue(first["confirmed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(delegate.calls), 1)

    def test_wrapper_validates_pseudonymous_subject_and_canonical_scope_ids(self):
        delegate = Delegate()
        wrapper = self.wrapper(delegate)
        rejected = wrapper.delete_subject_data(
            subject_ref="person@example.com",
            project_id="film",
            tenant_id="tenant-1",
            request_id="request-id-011",
        )
        self.assertEqual(rejected["status"], "FAILED")
        self.assertEqual(delegate.calls, [])

        accepted = wrapper.delete_subject_data(
            subject_ref="s-1",
            project_id="film",
            tenant_id="tenant-1",
            request_id="request-id-012",
        )
        self.assertTrue(accepted["confirmed"])
        self.assertEqual(len(delegate.calls), 1)

        invalid_purge = wrapper.purge_expired_data(
            cutoff=datetime(2026, 11, 1, tzinfo=timezone.utc),
            project_id="",
            tenant_id="tenant-1",
            request_id="request-id-013",
        )
        self.assertEqual(invalid_purge["status"], "FAILED")
        self.assertEqual(len(delegate.calls), 1)

    def test_ledger_file_is_private_on_posix(self):
        self.wrapper(Delegate())
        if os.name == "posix":
            self.assertEqual((self.root / "dedupe.sqlite").stat().st_mode & 0o777, 0o600)


class SQLitePrivacyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "application.sqlite"
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE subjects (id INTEGER PRIMARY KEY, subject_ref TEXT NOT NULL, "
            "project_id TEXT NOT NULL, tenant_id TEXT, expires_at TEXT, payload TEXT)"
        )
        db.executemany(
            "INSERT INTO subjects(subject_ref,project_id,tenant_id,expires_at,payload) VALUES(?,?,?,?,?)",
            [
                ("subject-ref-01", "project-id-01", "tenant-id-01", "2026-10-01T00:00:00+00:00", "private-a"),
                ("subject-ref-01", "project-id-01", "tenant-id-02", "2026-10-01T00:00:00+00:00", "private-b"),
                ("subject-ref-01", "project-id-02", "tenant-id-01", "2026-10-01T00:00:00+00:00", "private-c"),
                ("subject-ref-02", "project-id-01", "tenant-id-01", "2026-12-01T00:00:00+00:00", "private-d"),
            ],
        )
        db.commit()
        db.close()

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, **kwargs):
        options = {
            "table": "subjects",
            "digest_key_provider": lambda context: b"s" * 32,
        }
        options.update(kwargs)
        return SQLitePrivacyDataAdapter(self.path, **options)

    def rows(self):
        db = sqlite3.connect(self.path)
        rows = db.execute(
            "SELECT subject_ref,project_id,tenant_id,payload FROM subjects ORDER BY id"
        ).fetchall()
        db.close()
        return rows

    def test_exact_scope_delete_and_transactional_replay(self):
        args = {
            "subject_ref": "subject-ref-01",
            "project_id": "project-id-01",
            "tenant_id": "tenant-id-01",
            "request_id": "request-id-101",
        }
        self.assertEqual(self.adapter().delete_subject_data(**args)["deleted_count"], 1)
        replay = self.adapter().delete_subject_data(**args)
        self.assertTrue(replay["replayed"])
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertIn(("subject-ref-01", "project-id-01", "tenant-id-02", "private-b"), rows)
        self.assertIn(("subject-ref-01", "project-id-02", "tenant-id-01", "private-c"), rows)

    def test_same_request_id_different_tenant_is_rejected(self):
        args = {
            "subject_ref": "subject-ref-01",
            "project_id": "project-id-01",
            "tenant_id": "tenant-id-01",
            "request_id": "request-id-102",
        }
        self.adapter().delete_subject_data(**args)
        args["tenant_id"] = "tenant-id-02"
        result = self.adapter().delete_subject_data(**args)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(len(self.rows()), 3)

    def test_retention_purges_only_expired_rows_within_exact_scope(self):
        result = self.adapter().purge_expired_data(
            cutoff=datetime(2026, 11, 1, tzinfo=timezone.utc),
            project_id="project-id-01",
            tenant_id="tenant-id-01",
            request_id="request-id-103",
        )
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["deleted_count"], 1)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertIn(("subject-ref-01", "project-id-01", "tenant-id-02", "private-b"), rows)
        self.assertIn(("subject-ref-02", "project-id-01", "tenant-id-01", "private-d"), rows)

    def test_naive_cutoff_and_invalid_scope_are_rejected(self):
        result = self.adapter().purge_expired_data(
            cutoff=datetime(2026, 11, 1),
            project_id="project-id-01",
            tenant_id="tenant-id-01",
            request_id="request-id-104",
        )
        self.assertEqual(result["status"], "FAILED")
        invalid = self.adapter().delete_subject_data(
            subject_ref="short",
            project_id="project-id-01",
            tenant_id="tenant-id-01",
            request_id="request-id-105",
        )
        self.assertEqual(invalid["status"], "FAILED")
        self.assertEqual(len(self.rows()), 4)

    def test_excessive_delete_batch_is_not_partially_deleted(self):
        with self.assertRaises(PrivacyAdapterConfigurationError):
            self.adapter(max_deleted_rows=0)
        adapter = self.adapter(max_deleted_rows=1)
        # Two tenants match only when the tenant scope is intentionally null
        # and the table contains two null-tenant rows.
        db = sqlite3.connect(self.path)
        db.executemany(
            "INSERT INTO subjects(subject_ref,project_id,tenant_id,expires_at,payload) VALUES(?,?,?,?,?)",
            [
                ("subject-ref-03", "project-id-03", None, None, "private-e"),
                ("subject-ref-03", "project-id-03", None, None, "private-f"),
            ],
        )
        db.commit()
        db.close()
        result = adapter.delete_subject_data(
            subject_ref="subject-ref-03",
            project_id="project-id-03",
            tenant_id=None,
            request_id="request-id-106",
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(len(self.rows()), 6)

    def test_short_scope_identifiers_match_privacy_lifecycle_contract(self):
        db = sqlite3.connect(self.path)
        db.execute(
            "INSERT INTO subjects(subject_ref,project_id,tenant_id,expires_at,payload) VALUES(?,?,?,?,?)",
            ("s-1", "film", "tenant-1", "2026-12-01T00:00:00+00:00", "short-scope-private"),
        )
        db.commit()
        db.close()
        result = self.adapter().delete_subject_data(
            subject_ref="s-1",
            project_id="film",
            tenant_id="tenant-1",
            request_id="request-short-001",
        )
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(len(self.rows()), 4)

    def test_missing_required_table_columns_fail_startup(self):
        db = sqlite3.connect(self.path)
        db.execute("CREATE TABLE incomplete (subject_ref TEXT)")
        db.commit()
        db.close()
        with self.assertRaises(PrivacyAdapterConfigurationError):
            self.adapter(table="incomplete")

    def test_identifier_key_is_stable_and_bound_to_metadata(self):
        adapter = self.adapter(adapter_id="stable-db")
        with self.assertRaises(PrivacyAdapterConfigurationError):
            SQLitePrivacyDataAdapter(
                self.path,
                table="subjects",
                adapter_id="stable-db",
                digest_key_provider=lambda context: b"x" * 32,
            )


class HTTPSPrivacyProviderAdapterTests(unittest.TestCase):
    def make(self, opener, **kwargs):
        options = {
            "delete_url": "https://privacy.example.test/v1/delete",
            "purge_url": "https://privacy.example.test/v1/purge",
            "allowed_hosts": ["privacy.example.test"],
            "authorization_header": lambda: "Bearer deployment-managed-value",
            "opener": opener,
        }
        options.update(kwargs)
        return HTTPSPrivacyProviderAdapter(**options)

    def test_documented_https_endpoint_sends_idempotency_and_sanitizes_response(self):
        response = FakeResponse(
            200,
            json.dumps({
                "confirmed": True, "status": "DELETED", "deleted_count": 2,
                "provider_subject": "must-not-escape",
            }).encode(),
        )
        opener = FakeOpener(response)
        adapter = self.make(opener)
        result = adapter.delete_subject_data(
            subject_ref="subject-ref-201", project_id="project-id-01",
            tenant_id="tenant-id-01", request_id="request-id-201",
        )
        self.assertEqual(result, {"confirmed": True, "status": "DELETED", "deleted_count": 2})
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_header("Idempotency-key"), "request-id-201")
        self.assertEqual(request.get_header("Authorization"), "Bearer deployment-managed-value")
        self.assertEqual(timeout, 5.0)
        self.assertTrue(response.closed)
        self.assertNotIn("must-not-escape", repr(result))

    def test_202_requires_pending_and_never_claims_completion(self):
        response = FakeResponse(
            202,
            b'{"confirmed":false,"status":"PENDING","deleted_count":0}',
        )
        result = self.make(FakeOpener(response)).delete_subject_data(
            subject_ref="subject-ref-202", project_id="project-id-01",
            tenant_id=None, request_id="request-id-202",
        )
        self.assertEqual(result["status"], "PENDING")
        self.assertFalse(result["confirmed"])

    def test_redirect_or_network_error_fails_closed(self):
        opener = FakeOpener(error=HTTPError(
            "https://privacy.example.test/v1/delete", 302, "redirect", {}, None
        ))
        result = self.make(opener).delete_subject_data(
            subject_ref="subject-ref-203", project_id="project-id-01",
            tenant_id=None, request_id="request-id-203",
        )
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn("redirect", repr(result))

    def test_http_unlisted_host_credentials_and_nonstandard_port_are_rejected(self):
        cases = [
            {"delete_url": "http://privacy.example.test/delete"},
            {"delete_url": "https://evil.example.test/delete"},
            {"delete_url": "https://user@privacy.example.test/delete"},
            {"delete_url": "https://privacy.example.test:8443/delete"},
        ]
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaises(PrivacyAdapterConfigurationError):
                    self.make(FakeOpener(), **options)

    def test_oversized_response_and_malformed_body_fail_closed(self):
        for body in (b"x" * 129, b"not-json"):
            result = self.make(
                FakeOpener(FakeResponse(200, body)),
                max_response_bytes=128,
            ).delete_subject_data(
                subject_ref="subject-ref-204", project_id="project-id-01",
                tenant_id=None, request_id="request-id-204",
            )
            self.assertEqual(result["status"], "FAILED")


class PrivacyRetentionJobTests(unittest.TestCase):
    class Service:
        def __init__(self, complete=True):
            self.complete = complete
            self.calls = []

        def purge_expired_privacy_data(self, **kwargs):
            self.calls.append(dict(kwargs))
            return {
                "status": "RETENTION_PURGE_CONFIRMED" if self.complete else "RETENTION_PURGE_PARTIAL",
                "confirmed": self.complete,
                "completed": self.complete,
                "store_count": 2,
                "deleted_count": 4 if self.complete else 0,
            }

    def test_daily_run_uses_fixed_scopes_and_stable_ids_for_scheduler_retries(self):
        service = self.Service()
        now = datetime(2026, 11, 5, 17, 30, tzinfo=timezone(timedelta(hours=3)))
        job = PrivacyRetentionJob(
            service,
            principal_provider=lambda: "service-principal-01",
            scopes=[{"project_id": "project-id-01", "tenant_id": "tenant-id-01"}],
            retention_days=30,
            clock=lambda: now,
        )
        first, second = job.run_once(), job.run_once()
        self.assertTrue(first["completed"])
        self.assertTrue(second["completed"])
        self.assertEqual(service.calls[0]["request_id"], service.calls[1]["request_id"])
        self.assertEqual(service.calls[0]["principal_id"], "service-principal-01")
        self.assertEqual(service.calls[0]["cutoff"], datetime(2026, 10, 6, tzinfo=timezone.utc))

    def test_multiple_tenants_are_fixed_and_reported_without_identifiers(self):
        service = self.Service()
        job = PrivacyRetentionJob(
            service,
            principal_provider=lambda: "service-principal-01",
            scopes=[
                {"project_id": "project-id-01", "tenant_id": "tenant-id-01"},
                {"project_id": "project-id-01", "tenant_id": "tenant-id-02"},
            ],
            retention_days=7,
            clock=lambda: datetime(2026, 11, 5, tzinfo=timezone.utc),
        )
        result = job.run_once()
        self.assertTrue(result["completed"])
        self.assertEqual(result["scope_count"], 2)
        self.assertEqual(service.calls[0]["tenant_id"], "tenant-id-01")
        self.assertEqual(service.calls[1]["tenant_id"], "tenant-id-02")
        self.assertNotIn("tenant-id-01", repr(result))

    def test_partial_scope_result_is_never_reported_as_complete(self):
        service = self.Service(complete=False)
        job = PrivacyRetentionJob(
            service,
            principal_provider=lambda: "service-principal-01",
            scopes=[{"project_id": "project-id-01", "tenant_id": None}],
            retention_days=7,
            clock=lambda: datetime(2026, 11, 5, tzinfo=timezone.utc),
        )
        result = job.run_once()
        self.assertFalse(result["completed"])
        self.assertEqual(result["status"], "RETENTION_JOB_PARTIAL")

    def test_invalid_principal_clock_and_duplicate_scopes_fail_closed(self):
        service = self.Service()
        options = {
            "service": service,
            "scopes": [{"project_id": "project-id-01", "tenant_id": None}],
            "retention_days": 30,
        }
        job = PrivacyRetentionJob(
            **options,
            principal_provider=lambda: "",
            clock=lambda: datetime(2026, 11, 5, tzinfo=timezone.utc),
        )
        self.assertFalse(job.run_once()["completed"])
        self.assertEqual(service.calls, [])
        bad_clock = PrivacyRetentionJob(
            **options,
            principal_provider=lambda: "service-principal-01",
            clock=lambda: datetime(2026, 11, 5),
        )
        self.assertEqual(bad_clock.run_once()["status"], "RETENTION_JOB_CLOCK_INVALID")
        with self.assertRaises(PrivacyAdapterConfigurationError):
            PrivacyRetentionJob(
                service,
                principal_provider=lambda: "service-principal-01",
                scopes=[
                    {"project_id": "project-id-01", "tenant_id": None},
                    {"project_id": "project-id-01", "tenant_id": None},
                ],
                retention_days=7,
            )


if __name__ == "__main__":
    unittest.main()
