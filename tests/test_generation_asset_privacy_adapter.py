import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from projects.generation.privacy_asset_adapter import GenerationAssetPrivacyAdapter
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
        state = {
            **self.scope(),
            **root_scope,
            "states": states if states is not None else [
                {
                    "asset_id": row["asset_id"],
                    "project_id": row.get("project_id", PROJECT),
                    "tenant_id": row.get("tenant_id", TENANT),
                    "subject_ref": row.get("subject_ref", SUBJECT),
                    "output_file": row.get("file_path"),
                }
                for row in assets
            ],
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

    def test_never_deletes_planned_output_paths(self):
        planned = self.images / "planned.png"
        planned.write_bytes(b"not-yet-generated")
        self.write_documents([{
            "asset_id": "planned",
            "subject_ref": SUBJECT,
            "file_path": None,
            "planned_output_path": "output/images/planned.png",
        }])
        result = self.adapter.delete_subject_data(
            subject_ref=SUBJECT, project_id=PROJECT, tenant_id=TENANT, request_id=REQUEST
        )
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["deleted_count"], 2)
        self.assertTrue(planned.exists())

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


if __name__ == "__main__":
    unittest.main()
