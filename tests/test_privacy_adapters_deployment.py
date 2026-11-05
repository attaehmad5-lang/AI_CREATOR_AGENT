from datetime import datetime, timedelta, timezone
import json
import sqlite3
import urllib.error

import pytest

from projects.provider_store.privacy_adapters import (
    HTTPSPrivacyProviderAdapter,
    PersistentPrivacyAdapter,
    PrivacyAdapterConfigurationError,
    PrivacyRetentionJob,
    SQLitePrivacyDataAdapter,
    create_privacy_lifecycle_manager,
)


KEY = b"k" * 32
PROJECT = "project-123"
TENANT = "tenant-123"
SUBJECT = "subject-123"
REQUEST = "request-123"


def key_provider(_context):
    return KEY


class RecordingAdapter:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"confirmed": True, "status": "DELETED", "deleted_count": 1}

    def delete_subject_data(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return self.result

    def purge_expired_data(self, **kwargs):
        self.calls.append(("purge", kwargs))
        return {"confirmed": True, "status": "PURGED", "deleted_count": 2}


def test_sqlite_adapter_deletes_only_exact_tenant_project_scope(tmp_path):
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records(subject_ref TEXT, project_id TEXT, tenant_id TEXT, expires_at TEXT)")
    con.executemany("INSERT INTO records VALUES (?, ?, ?, ?)", [
        (SUBJECT, PROJECT, TENANT, None),
        (SUBJECT, PROJECT, "tenant-other", None),
        (SUBJECT, "project-other", TENANT, None),
    ])
    con.commit()
    con.close()
    adapter = SQLitePrivacyDataAdapter(db, table="records", digest_key_provider=key_provider)
    result = adapter.delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    assert result == {"confirmed": True, "status": "DELETED", "deleted_count": 1}
    replay = adapter.delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    assert replay["confirmed"] is True and replay["replayed"] is True
    con = sqlite3.connect(db)
    rows = con.execute("SELECT subject_ref, project_id, tenant_id FROM records ORDER BY tenant_id").fetchall()
    con.close()
    assert rows == [(SUBJECT, "project-other", TENANT), (SUBJECT, PROJECT, "tenant-other")]


def test_sqlite_adapter_fails_closed_when_request_id_reused_for_other_scope(tmp_path):
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records(subject_ref TEXT, project_id TEXT, tenant_id TEXT, expires_at TEXT)")
    con.commit()
    con.close()
    adapter = SQLitePrivacyDataAdapter(db, table="records", digest_key_provider=key_provider)
    assert adapter.delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)["confirmed"]
    changed = adapter.delete_subject_data(subject_ref="subject-other", project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    assert changed["confirmed"] is False


def test_sqlite_adapter_purges_only_expired_rows_and_keeps_future_rows(tmp_path):
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records(subject_ref TEXT, project_id TEXT, tenant_id TEXT, expires_at TEXT)")
    now = datetime.now(timezone.utc)
    con.executemany("INSERT INTO records VALUES (?, ?, ?, ?)", [
        (SUBJECT, PROJECT, TENANT, (now - timedelta(days=2)).isoformat(timespec="microseconds")),
        ("subject-future", PROJECT, TENANT, (now + timedelta(days=2)).isoformat(timespec="microseconds")),
        ("subject-other", PROJECT, "tenant-other", (now - timedelta(days=2)).isoformat(timespec="microseconds")),
    ])
    con.commit()
    con.close()
    adapter = SQLitePrivacyDataAdapter(db, table="records", digest_key_provider=key_provider)
    result = adapter.purge_expired_data(cutoff=now, project_id=PROJECT, tenant_id=TENANT, request_id="purge-request-1")
    assert result == {"confirmed": True, "status": "PURGED", "deleted_count": 1}


def test_sqlite_adapter_rejects_naive_cutoff_and_invalid_scope(tmp_path):
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records(subject_ref TEXT, project_id TEXT, tenant_id TEXT, expires_at TEXT)")
    con.commit()
    con.close()
    adapter = SQLitePrivacyDataAdapter(db, table="records", digest_key_provider=key_provider)
    assert adapter.purge_expired_data(cutoff=datetime.now(), project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)["confirmed"] is False
    assert adapter.delete_subject_data(subject_ref=SUBJECT, project_id="bad", tenant_id=TENANT, request_id=REQUEST)["confirmed"] is False


def test_sqlite_adapter_requires_expected_schema_and_stable_key(tmp_path):
    db = tmp_path / "app.db"
    sqlite3.connect(db).execute("CREATE TABLE records(id INTEGER)").connection.commit()
    with pytest.raises(PrivacyAdapterConfigurationError):
        SQLitePrivacyDataAdapter(db, table="records", digest_key_provider=key_provider)
    with pytest.raises(PrivacyAdapterConfigurationError):
        SQLitePrivacyDataAdapter(tmp_path / "other.db", table="records", digest_key_provider=lambda _: b"x")


def test_persistent_adapter_replays_success_without_repeating_external_delete(tmp_path):
    wrapped = RecordingAdapter()
    adapter = PersistentPrivacyAdapter(wrapped, ledger_path=tmp_path / "ledger.db", adapter_id="provider-a", digest_key_provider=key_provider)
    args = dict(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    first = adapter.delete_subject_data(**args)
    second = adapter.delete_subject_data(**args)
    assert first["confirmed"] is True
    assert second["replayed"] is True
    assert len(wrapped.calls) == 1


def test_persistent_adapter_does_not_store_raw_subject_or_tenant(tmp_path):
    wrapped = RecordingAdapter()
    ledger = tmp_path / "ledger.db"
    adapter = PersistentPrivacyAdapter(wrapped, ledger_path=ledger, adapter_id="provider-a", digest_key_provider=key_provider)
    adapter.delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    raw = ledger.read_bytes()
    assert SUBJECT.encode() not in raw
    assert TENANT.encode() not in raw
    assert PROJECT.encode() not in raw


def test_persistent_adapter_key_rotation_fails_closed(tmp_path):
    current = [KEY]
    wrapped = RecordingAdapter()
    adapter = PersistentPrivacyAdapter(wrapped, ledger_path=tmp_path / "ledger.db", adapter_id="provider-a", digest_key_provider=lambda _: current[0])
    current[0] = b"z" * 32
    result = adapter.delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    assert result["confirmed"] is False
    assert wrapped.calls == []


def test_persistent_adapter_rejects_symlink_ledger(tmp_path):
    target = tmp_path / "target.db"
    target.touch()
    link = tmp_path / "ledger.db"
    link.symlink_to(target)
    with pytest.raises(PrivacyAdapterConfigurationError):
        PersistentPrivacyAdapter(RecordingAdapter(), ledger_path=link, adapter_id="provider-a", digest_key_provider=key_provider)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self.payload[:size]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse(b'{"confirmed":true,"status":"DELETED","deleted_count":1}')
        self.error = error
        self.request = None

    def open(self, request, timeout):
        self.request = request
        if self.error:
            raise self.error
        return self.response


def make_https(opener):
    return HTTPSPrivacyProviderAdapter(
        delete_url="https://privacy.example.test/v1/delete",
        purge_url="https://privacy.example.test/v1/purge",
        allowed_hosts=["privacy.example.test"],
        authorization_header=lambda: "Bearer test-secret",
        opener=opener,
    )


def test_https_adapter_sends_scoped_payload_and_idempotency_header():
    opener = FakeOpener()
    adapter = make_https(opener)
    result = adapter.delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    assert result["confirmed"] is True
    assert opener.request.get_header("Idempotency-key") == REQUEST
    assert json.loads(opener.request.data)["tenant_id"] == TENANT
    assert opener.request.get_header("Authorization") == "Bearer test-secret"


def test_https_adapter_rejects_http_hosts_credentials_ports_and_redirects():
    for url in (
        "http://privacy.example.test/delete",
        "https://attacker.example.test/delete",
        "https://user:pass@privacy.example.test/delete",
        "https://privacy.example.test:8443/delete",
    ):
        with pytest.raises(PrivacyAdapterConfigurationError):
            HTTPSPrivacyProviderAdapter(
                delete_url=url,
                purge_url="https://privacy.example.test/purge",
                allowed_hosts=["privacy.example.test"],
                authorization_header=lambda: "Bearer x",
            )
    opener = FakeOpener(error=urllib.error.HTTPError("https://privacy.example.test", 302, "redirect", {}, None))
    result = make_https(opener).delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    assert result["confirmed"] is False


def test_https_adapter_bounds_response_and_rejects_malformed_or_unconfirmed_response():
    oversized = FakeOpener(FakeResponse(b"x" * 70000))
    assert make_https(oversized).delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)["confirmed"] is False
    malformed = FakeOpener(FakeResponse(b'{"confirmed":true,"status":"FAILED","deleted_count":1}'))
    assert make_https(malformed).delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)["confirmed"] is False
    pending = FakeOpener(FakeResponse(b'{"confirmed":false,"status":"PENDING","deleted_count":0}', 202))
    assert make_https(pending).delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)["status"] == "PENDING"


class FakePrivacyService:
    def __init__(self):
        self.calls = []

    def purge_expired_privacy_data(self, **kwargs):
        self.calls.append(kwargs)
        return {"confirmed": True, "completed": True, "status": "PRIVACY_PURGE_CONFIRMED", "deleted_count": 3, "store_count": 2}


def test_retention_job_runs_each_fixed_scope_with_admin_confirmation():
    service = FakePrivacyService()
    fixed_now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    job = PrivacyRetentionJob(
        service,
        principal_provider=lambda: "retention-service",
        scopes=[{"project_id": PROJECT, "tenant_id": TENANT}, {"project_id": PROJECT, "tenant_id": None}],
        retention_days=30,
        clock=lambda: fixed_now,
        request_id_factory=lambda: "retention-request-123",
    )
    result = job.run_once()
    assert result["completed"] is True
    assert result["completed_count"] == 2
    assert all(call["principal_id"] == "retention-service" and call["confirmed"] is True for call in service.calls)
    assert all(call["cutoff"] == fixed_now - timedelta(days=30) for call in service.calls)


def test_retention_job_fails_closed_on_missing_principal_or_partial_store_failure():
    service = FakePrivacyService()
    no_principal = PrivacyRetentionJob(service, principal_provider=lambda: "", scopes=[{"project_id": PROJECT, "tenant_id": TENANT}], retention_days=30)
    assert no_principal.run_once()["completed"] is False
    class PartialService(FakePrivacyService):
        def purge_expired_privacy_data(self, **kwargs):
            return {"confirmed": False, "completed": False, "status": "PRIVACY_PURGE_PENDING", "deleted_count": 0, "store_count": 1}
    partial = PrivacyRetentionJob(PartialService(), principal_provider=lambda: "retention-service", scopes=[{"project_id": PROJECT, "tenant_id": TENANT}], retention_days=30)
    assert partial.run_once()["status"] == "RETENTION_JOB_PARTIAL"


def test_retention_job_rejects_unsafe_configuration():
    with pytest.raises(PrivacyAdapterConfigurationError):
        PrivacyRetentionJob(FakePrivacyService(), principal_provider=lambda: "svc", scopes=[], retention_days=30)
    with pytest.raises(PrivacyAdapterConfigurationError):
        PrivacyRetentionJob(FakePrivacyService(), principal_provider=lambda: "svc", scopes=[{"project_id": "short"}], retention_days=30)
    with pytest.raises(PrivacyAdapterConfigurationError):
        PrivacyRetentionJob(FakePrivacyService(), principal_provider=lambda: "svc", scopes=[{"project_id": PROJECT, "tenant_id": TENANT}], retention_days=0)


def test_lifecycle_factory_wires_external_adapters_with_durable_idempotency(tmp_path):
    wrapped = RecordingAdapter()
    manager = create_privacy_lifecycle_manager(
        {"provider-a": wrapped},
        ledger_directory=tmp_path / "privacy-ledgers",
        digest_key_provider=key_provider,
    )
    first = manager.delete_subject_data(
        subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT,
        request_id=REQUEST, confirmed=True,
    )
    replay = manager.delete_subject_data(
        subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT,
        request_id=REQUEST, confirmed=True,
    )
    assert first["completed"] is True and replay["completed"] is True
    assert first["stores"]["provider-a"]["confirmed"] is True
    assert len(wrapped.calls) == 1
    ledger = tmp_path / "privacy-ledgers" / "provider-a.sqlite3"
    assert ledger.is_file()


def test_lifecycle_factory_keeps_transactional_sqlite_adapter_unwrapped(tmp_path):
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE records(subject_ref TEXT, project_id TEXT, tenant_id TEXT, expires_at TEXT)")
    con.commit()
    con.close()
    adapter = SQLitePrivacyDataAdapter(db, table="records", digest_key_provider=key_provider)
    manager = create_privacy_lifecycle_manager(
        {"app-db": adapter}, ledger_directory=tmp_path / "privacy-ledgers", digest_key_provider=key_provider
    )
    assert manager._stores["app-db"] is adapter


def test_lifecycle_factory_rejects_insecure_or_incomplete_configuration(tmp_path):
    with pytest.raises(PrivacyAdapterConfigurationError):
        create_privacy_lifecycle_manager({}, ledger_directory=tmp_path / "ledgers", digest_key_provider=key_provider)
    bad_dir = tmp_path / "ledger-link"
    target = tmp_path / "target-dir"
    target.mkdir()
    bad_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(PrivacyAdapterConfigurationError):
        create_privacy_lifecycle_manager(
            {"provider-a": RecordingAdapter()}, ledger_directory=bad_dir, digest_key_provider=key_provider
        )


def test_https_adapter_rejects_http_200_pending_response():
    pending = FakeOpener(FakeResponse(b'{"confirmed":false,"status":"PENDING","deleted_count":0}', 200))
    result = make_https(pending).delete_subject_data(subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST)
    assert result["confirmed"] is False and result["status"] == "FAILED"


def test_https_adapter_rejects_malformed_dns_host_labels():
    for host in ("-bad.privacy.example.test", "bad-.privacy.example.test", "a..privacy.example.test", "privacy.example.test."):
        with pytest.raises(PrivacyAdapterConfigurationError):
            HTTPSPrivacyProviderAdapter(
                delete_url=f"https://{host}/delete",
                purge_url="https://privacy.example.test/purge",
                allowed_hosts=[host, "privacy.example.test"],
                authorization_header=lambda: "Bearer x",
            )


def test_retention_job_preserves_opaque_oidc_principal_capability():
    service = FakePrivacyService()
    class OpaquePrincipal:
        pass
    principal = OpaquePrincipal()
    job = PrivacyRetentionJob(
        service, principal_provider=lambda: principal,
        scopes=[{"project_id": PROJECT, "tenant_id": TENANT}], retention_days=30,
    )
    result = job.run_once()
    assert result["completed"] is True
    assert service.calls[0]["principal_id"] is principal
