import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.generation.privacy_asset_adapter import (
    GenerationAssetPrivacyAdapter,
    GenerationAssetPrivacyError,
    copy_generation_privacy_scope,
)
from projects.provider_store.privacy_adapters import create_privacy_lifecycle_manager


PROJECT = "project-123"
TENANT = "tenant-123"
SUBJECT = "subject-123"
REQUEST = "asset-delete-001"


class GenerationAssetPrivacyAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.images = self.root / "output" / "images"
        self.images.mkdir(parents=True)
        self.manifest = self.root / "asset_manifest.json"
        self.state = self.root / "generation_state.json"
        self.adapter = GenerationAssetPrivacyAdapter(
            self.manifest,
            asset_roots={"output/images": self.images},
            state_paths=[self.state],
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def scope(subject=SUBJECT, tenant=TENANT):
        return {
            "project_id": PROJECT,
            "tenant_id": tenant,
            "subject_ref": subject,
        }

    def write_documents(self, assets, states=None, **root_scope):
        manifest = {
            **self.scope(),
            **root_scope,
            "assets": assets,
        }
        state_rows = []
        for row in assets:
            state_row = {
                "asset_id": row["asset_id"],
                "project_id": row.get("project_id", PROJECT),
                "tenant_id": row.get("tenant_id", TENANT),
                "subject_ref": row.get("subject_ref", SUBJECT),
                "output_file": row.get("file_path"),
                "planned_output_path": row.get("planned_output_path"),
            }
            if "expires_at" in row:
                state_row["expires_at"] = row["expires_at"]
            state_rows.append(state_row)
        state = {
            **self.scope(),
            **root_scope,
            "states": states if states is not None else state_rows,
        }
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.state.write_text(json.dumps(state), encoding="utf-8")

    def test_deletes_only_exact_subject_tenant_and_project_assets(self):
        target = self.images / "target.png"
        other_subject = self.images / "other-subject.png"
        other_tenant = self.images / "other-tenant.png"
        for path in (target, other_subject, other_tenant):
            path.write_bytes(b"asset")

        self.write_documents([
            {"asset_id": "target", "subject_ref": SUBJECT, "file_path": "output/images/target.png"},
            {"asset_id": "other-subject", "subject_ref": "subject-other", "file_path": "output/images/other-subject.png"},
            {"asset_id": "other-tenant", "subject_ref": SUBJECT, "tenant_id": "tenant-other", "file_path": "output/images/other-tenant.png"},
        ])
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertEqual(result, {"confirmed": True, "status": "DELETED", "deleted_count": 2})
        self.assertFalse(target.exists())
        self.assertTrue(other_subject.exists())
        self.assertTrue(other_tenant.exists())
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual({row["asset_id"] for row in manifest["assets"]}, {"other-subject", "other-tenant"})
        self.assertEqual({row["asset_id"] for row in state["states"]}, {"other-subject", "other-tenant"})

    def test_legacy_unscoped_manifest_fails_closed_without_deleting(self):
        asset = self.images / "legacy.png"
        asset.write_bytes(b"keep")
        self.manifest.write_text(json.dumps({
            "project": "Old Project",
            "assets": [{"asset_id": "legacy", "file_path": "output/images/legacy.png"}],
        }), encoding="utf-8")
        self.state.write_text(json.dumps({
            "states": [{"asset_id": "legacy", "output_file": "output/images/legacy.png"}],
        }), encoding="utf-8")
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(asset.exists())

    def test_untracked_planned_output_blocks_deletion_without_deleting_file(self):
        planned = self.images / "planned.png"
        planned.write_bytes(b"not-yet-generated")
        self.write_documents([{
            "asset_id": "planned",
            "subject_ref": SUBJECT,
            "file_path": None,
            "planned_output_path": "output/images/planned.png",
        }])
        audit = self.adapter.audit()
        self.assertEqual(audit["status"], "ASSET_PRIVACY_MIGRATION_REQUIRED")
        self.assertEqual(audit["untracked_planned_count"], 2)
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertFalse(result["confirmed"])
        self.assertTrue(planned.exists())
        self.assertTrue(json.loads(self.manifest.read_text(encoding="utf-8"))["assets"])

    def test_missing_planned_output_does_not_block_record_deletion(self):
        self.write_documents([{
            "asset_id": "planned",
            "subject_ref": SUBJECT,
            "file_path": None,
            "planned_output_path": "output/images/not-created.png",
        }])
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["deleted_count"], 2)

    def test_actual_file_matching_planned_path_can_be_deleted(self):
        actual = self.images / "actual.png"
        actual.write_bytes(b"actual")
        self.write_documents([{
            "asset_id": "actual",
            "subject_ref": SUBJECT,
            "file_path": "output/images/actual.png",
            "planned_output_path": "output/images/actual.png",
        }])
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertTrue(result["confirmed"])
        self.assertFalse(actual.exists())
        self.assertEqual(self.adapter.audit()["status"], "ASSET_PRIVACY_READY")

    def test_path_traversal_is_rejected_before_any_mutation(self):
        asset = self.images / "safe.png"
        asset.write_bytes(b"keep")
        self.write_documents([{
            "asset_id": "traversal",
            "subject_ref": SUBJECT,
            "file_path": "output/images/../safe.png",
        }])
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertFalse(result["confirmed"])
        self.assertTrue(asset.exists())
        self.assertTrue(json.loads(self.manifest.read_text(encoding="utf-8"))["assets"])

    def test_shared_file_reference_blocks_deletion(self):
        shared = self.images / "shared.png"
        shared.write_bytes(b"shared")
        self.write_documents([
            {"asset_id": "target", "subject_ref": SUBJECT, "file_path": "output/images/shared.png"},
            {"asset_id": "other", "subject_ref": "subject-other", "file_path": "output/images/shared.png"},
        ])
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertFalse(result["confirmed"])
        self.assertTrue(shared.exists())

    def test_retention_purges_only_expired_exact_scope_and_requires_expiry(self):
        expired = self.images / "expired.png"
        future = self.images / "future.png"
        other_tenant = self.images / "other-tenant.png"
        for path in (expired, future, other_tenant):
            path.write_bytes(b"asset")
        self.write_documents([
            {"asset_id": "expired", "subject_ref": SUBJECT, "expires_at": "2025-12-31T00:00:00+00:00", "file_path": "output/images/expired.png"},
            {"asset_id": "future", "subject_ref": SUBJECT, "expires_at": "2027-01-01T00:00:00+00:00", "file_path": "output/images/future.png"},
            {"asset_id": "other-tenant", "subject_ref": SUBJECT, "tenant_id": "tenant-other", "expires_at": "2025-12-01T00:00:00+00:00", "file_path": "output/images/other-tenant.png"},
        ])
        result = self.adapter.purge_expired_data(
            cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
            project_id=PROJECT, tenant_id=TENANT, request_id="asset-purge-001",
        )
        self.assertEqual(result, {"confirmed": True, "status": "PURGED", "deleted_count": 2})
        self.assertFalse(expired.exists())
        self.assertTrue(future.exists())
        self.assertTrue(other_tenant.exists())

    def test_retention_fails_closed_when_scoped_record_has_no_expiry(self):
        asset = self.images / "no-expiry.png"
        asset.write_bytes(b"keep")
        self.write_documents([{
            "asset_id": "no-expiry",
            "subject_ref": SUBJECT,
            "file_path": "output/images/no-expiry.png",
        }])
        result = self.adapter.purge_expired_data(
            cutoff=datetime(2026, 1, 1, tzinfo=timezone.utc),
            project_id=PROJECT, tenant_id=TENANT, request_id="asset-purge-002",
        )
        self.assertFalse(result["confirmed"])
        self.assertTrue(asset.exists())

    def test_symlink_asset_is_rejected(self):
        target = self.root / "outside.png"
        target.write_bytes(b"outside")
        link = self.images / "link.png"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        self.write_documents([{
            "asset_id": "link",
            "subject_ref": SUBJECT,
            "file_path": "output/images/link.png",
        }])
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertFalse(result["confirmed"])
        self.assertTrue(target.exists())

    def test_persistent_factory_replays_success_without_repeating_deletion(self):
        asset = self.images / "replay.png"
        asset.write_bytes(b"asset")
        self.write_documents([{
            "asset_id": "replay",
            "subject_ref": SUBJECT,
            "file_path": "output/images/replay.png",
        }])
        manager = create_privacy_lifecycle_manager(
            {"generation-assets": self.adapter},
            ledger_directory=self.root / "privacy-ledger",
            digest_key_provider=lambda _context: b"k" * 32,
        )
        first = manager.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT,
            request_id="asset-delete-replay-001", confirmed=True,
        )
        second = manager.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT,
            request_id="asset-delete-replay-001", confirmed=True,
        )
        self.assertTrue(first["completed"])
        self.assertTrue(second["completed"])
        self.assertTrue(asset.is_file() is False)
        self.assertEqual(first["deleted_count"], 2)
        self.assertEqual(second["deleted_count"], 2)




    def test_generation_scope_helper_requires_complete_pseudonymous_scope(self):
        source = {"project_id": PROJECT, "tenant_id": None, "subject_ref": SUBJECT}
        self.assertEqual(copy_generation_privacy_scope(source), source)
        with self.assertRaises(GenerationAssetPrivacyError):
            copy_generation_privacy_scope({"project_id": PROJECT, "subject_ref": SUBJECT})
        with self.assertRaises(GenerationAssetPrivacyError):
            copy_generation_privacy_scope({"project_id": PROJECT, "tenant_id": TENANT, "subject_ref": "user@example.test"})
        with self.assertRaises(GenerationAssetPrivacyError):
            copy_generation_privacy_scope({"project_id": PROJECT, "tenant_id": TENANT, "subject_ref": "123456789"})


    def test_generation_scope_helper_applies_row_override_and_validates_expiry(self):
        document = {"project_id": PROJECT, "tenant_id": TENANT, "subject_ref": SUBJECT}
        row = {"tenant_id": "tenant-other", "subject_ref": "subject-other", "expires_at": "2026-01-01T00:00:00Z"}
        result = copy_generation_privacy_scope(document, row)
        self.assertEqual(result["tenant_id"], "tenant-other")
        self.assertEqual(result["subject_ref"], "subject-other")
        self.assertEqual(result["expires_at"], row["expires_at"])
        with self.assertRaises(GenerationAssetPrivacyError):
            copy_generation_privacy_scope(document, {"expires_at": "2026-01-01T00:00:00"})

    def test_privacy_audit_counts_scoped_and_retention_ready_records_without_paths(self):
        self.write_documents([
            {"asset_id": "one", "subject_ref": SUBJECT, "expires_at": "2026-12-01T00:00:00Z", "file_path": None},
            {"asset_id": "two", "subject_ref": "subject-other", "expires_at": "2027-01-01T00:00:00+00:00", "file_path": None},
        ])
        result = self.adapter.audit()
        self.assertEqual(result["status"], "ASSET_PRIVACY_READY")
        self.assertEqual(result["record_count"], 4)
        self.assertEqual(result["scoped_count"], 4)
        self.assertEqual(result["unscoped_count"], 0)
        self.assertEqual(result["retention_ready_count"], 4)
        self.assertNotIn(str(self.root), repr(result))


    def test_privacy_audit_marks_legacy_manifest_for_migration(self):
        self.manifest.write_text(json.dumps({"assets": [{"asset_id": "legacy"}]}), encoding="utf-8")
        self.state.write_text(json.dumps({"states": [{"asset_id": "legacy"}]}), encoding="utf-8")
        result = self.adapter.audit()
        self.assertEqual(result["status"], "ASSET_PRIVACY_MIGRATION_REQUIRED")
        self.assertEqual(result["unscoped_count"], 2)


    def test_production_mode_requires_a_deployment_key_provider(self):
        with self.assertRaises(GenerationAssetPrivacyError):
            GenerationAssetPrivacyAdapter(
                self.manifest, asset_roots={"output/images": self.images},
                require_encrypted_metadata=True,
            )


    def test_legacy_metadata_migration_encrypts_both_documents_and_hides_subject(self):
        self.write_documents([{
            "asset_id": "encrypted", "subject_ref": SUBJECT,
            "file_path": None, "planned_output_path": "output/images/not-created.png",
        }])
        key_provider = lambda _context: b"e" * 32
        encrypted = GenerationAssetPrivacyAdapter(
            self.manifest,
            asset_roots={"output/images": self.images},
            state_paths=[self.state],
            encryption_key_provider=key_provider,
            encryption_context="test-generation-assets-v1",
            require_encrypted_metadata=True,
            migrate_legacy_plaintext_metadata=True,
        )
        before = encrypted.audit()
        self.assertEqual(before["status"], "ASSET_PRIVACY_ENCRYPTION_MIGRATION_REQUIRED")
        self.assertEqual(before["encryption_migration_count"], 2)
        self.assertEqual(encrypted.migrate_legacy_metadata()["status"], "GENERATION_METADATA_MIGRATION_CONFIRMATION_REQUIRED")
        migrated = encrypted.migrate_legacy_metadata(confirmed=True)
        self.assertEqual(migrated["status"], "GENERATION_METADATA_ENCRYPTED")
        self.assertEqual(migrated["migrated_document_count"], 2)
        raw_manifest = self.manifest.read_bytes()
        raw_state = self.state.read_bytes()
        self.assertNotIn(SUBJECT.encode(), raw_manifest)
        self.assertNotIn(SUBJECT.encode(), raw_state)
        self.assertTrue(json.loads(raw_manifest)["format"].startswith("aca-encrypted"))
        self.assertEqual(encrypted.audit()["status"], "ASSET_PRIVACY_READY")
        self.assertEqual(encrypted.audit()["encrypted_document_count"], 2)


    def test_wrong_metadata_key_fails_closed_without_mutating_encrypted_documents(self):
        self.write_documents([{
            "asset_id": "encrypted", "subject_ref": SUBJECT,
            "file_path": None, "planned_output_path": "output/images/not-created.png",
        }])
        correct = GenerationAssetPrivacyAdapter(
            self.manifest, asset_roots={"output/images": self.images}, state_paths=[self.state],
            encryption_key_provider=lambda _context: b"c" * 32,
            encryption_context="test-generation-wrong-key-v1",
            require_encrypted_metadata=True, migrate_legacy_plaintext_metadata=True,
        )
        self.assertEqual(correct.migrate_legacy_metadata(confirmed=True)["status"], "GENERATION_METADATA_ENCRYPTED")
        before_manifest = self.manifest.read_bytes()
        before_state = self.state.read_bytes()
        wrong = GenerationAssetPrivacyAdapter(
            self.manifest, asset_roots={"output/images": self.images}, state_paths=[self.state],
            encryption_key_provider=lambda _context: b"w" * 32,
            encryption_context="test-generation-wrong-key-v1",
            require_encrypted_metadata=True,
        )
        self.assertEqual(wrong.audit()["status"], "ASSET_PRIVACY_AUDIT_FAILED")
        result = wrong.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST,
        )
        self.assertFalse(result["confirmed"])
        self.assertEqual(self.manifest.read_bytes(), before_manifest)
        self.assertEqual(self.state.read_bytes(), before_state)




    def test_authoritative_scope_migration_resolves_legacy_rows_and_updates_all_documents(self):
        self.manifest.write_text(json.dumps({"assets": [{"asset_id": "legacy-asset", "job_id": "job-legacy"}]}), encoding="utf-8")
        self.state.write_text(json.dumps({"states": [{"asset_id": "legacy-asset", "job_id": "job-legacy"}]}), encoding="utf-8")
        calls = []
        def resolver(lookup):
            calls.append(lookup)
            return {"project_id": PROJECT, "tenant_id": TENANT, "subject_ref": SUBJECT, "expires_at": "2027-01-01T00:00:00Z"}
        result = self.adapter.migrate_legacy_scope_metadata(scope_resolver=resolver, confirmed=True)
        self.assertEqual(result["status"], "GENERATION_SCOPE_METADATA_MIGRATED")
        self.assertEqual(result["migrated_record_count"], 2)
        self.assertEqual(len(calls), 2)
        audit = self.adapter.audit()
        self.assertEqual(audit["status"], "ASSET_PRIVACY_READY")
        self.assertEqual(audit["retention_ready_count"], 2)
        self.assertNotIn(SUBJECT, repr(result))
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8"))["assets"][0]["subject_ref"], SUBJECT)


    def test_authoritative_scope_migration_fails_closed_on_untrusted_resolver_result(self):
        manifest = {"assets": [{"asset_id": "legacy-asset", "job_id": "job-legacy"}]}
        state = {"states": [{"asset_id": "legacy-asset", "job_id": "job-legacy"}]}
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.state.write_text(json.dumps(state), encoding="utf-8")
        before_manifest = self.manifest.read_bytes()
        before_state = self.state.read_bytes()
        result = self.adapter.migrate_legacy_scope_metadata(
            scope_resolver=lambda _lookup: {"project_id": PROJECT, "tenant_id": TENANT, "subject_ref": "person@example.test"},
            confirmed=True,
        )
        self.assertEqual(result["status"], "GENERATION_SCOPE_MIGRATION_FAILED")
        self.assertEqual(self.manifest.read_bytes(), before_manifest)
        self.assertEqual(self.state.read_bytes(), before_state)


    def test_authoritative_scope_migration_requires_confirmation_and_resolver(self):
        no_confirmation = self.adapter.migrate_legacy_scope_metadata(scope_resolver=lambda _row: {}, confirmed=False)
        self.assertEqual(no_confirmation["status"], "GENERATION_SCOPE_MIGRATION_CONFIRMATION_REQUIRED")
        no_resolver = self.adapter.migrate_legacy_scope_metadata(scope_resolver=None, confirmed=True)
        self.assertEqual(no_resolver["status"], "GENERATION_SCOPE_RESOLVER_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
