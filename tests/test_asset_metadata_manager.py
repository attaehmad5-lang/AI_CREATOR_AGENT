import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from projects.provider_store.asset_metadata_manager import AssetMetadataManager


class AssetMetadataManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.manager = AssetMetadataManager(self.root / "asset-metadata.json")
        self.base = {
            "provider_id": "image-provider",
            "request_id": "request-001",
            "project_id": "film-project",
            "filename": "frame.png",
            "modality": "image",
            "mime_type": "image/png",
            "size_bytes": 128,
            "sha256": "a" * 64,
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _record(self, **overrides):
        values = {**self.base, **overrides}
        return self.manager.record_published_asset(**values)

    def test_published_asset_metadata_is_persisted_and_reloaded(self):
        result = self._record()
        self.assertTrue(result["saved"])
        self.assertEqual(result["status"], "RECORDED")
        self.assertEqual(result["record"]["provider_id"], "image-provider")
        self.assertEqual(result["record"]["request_id"], "request-001")
        self.assertEqual(result["record"]["project_id"], "film-project")
        self.assertEqual(result["record"]["filename"], "frame.png")
        self.assertEqual(result["record"]["sha256"], "a" * 64)
        reloaded = AssetMetadataManager(self.root / "asset-metadata.json")
        found = reloaded.get_asset(result["asset_id"])
        self.assertEqual(found["status"], "FOUND")
        self.assertEqual(found["record"], result["record"])
        self.assertNotIn(str(self.root), json.dumps(found))

    def test_duplicate_asset_record_is_idempotent(self):
        first = self._record(created_at="2026-10-15T12:00:00+00:00")
        duplicate = self._record(created_at="2026-10-15T13:00:00+00:00")
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(duplicate["status"], "ALREADY_RECORDED")
        self.assertEqual(len(self.manager.list_assets()["assets"]), 1)

    def test_conflicting_metadata_for_same_asset_identity_fails_closed(self):
        first = self._record()
        conflict = self._record(mime_type="image/jpeg")
        self.assertTrue(first["saved"])
        self.assertEqual(conflict["status"], "ASSET_METADATA_CONFLICT")

    def test_fifty_invalid_metadata_cases_are_rejected(self):
        invalid_cases = []
        for field in ("provider_id", "request_id", "project_id", "modality"):
            invalid_cases.extend((field, value) for value in ("", None, 7, "secret=never-store", "bad id"))
        invalid_cases.extend((("filename", value) for value in ("", None, "../frame.png", "/tmp/frame.png", "folder\\frame.png")))
        invalid_cases.extend((("mime_type", value) for value in ("", None, "image", "image/png\\nX: y", "secret=foo")))
        invalid_cases.extend((("size_bytes", value) for value in (0, -1, True, 1.5, 10**13)))
        invalid_cases.extend((("sha256", value) for value in ("", None, "0" * 63, "G" * 64, "a" * 65)))
        invalid_cases.extend((("created_at", value) for value in ("not-a-time", None, "2026-10-15T12:00:00", "", "secret=timestamp")))
        invalid_cases.extend((("mime_type", value) for value in ("application/json; charset=utf-8", "image/png\x00", "*/png", "x/y/z", 12)))
        self.assertEqual(len(invalid_cases), 50)
        for index, (field, value) in enumerate(invalid_cases):
            with self.subTest(index=index, field=field, value=repr(value)):
                result = self._record(**{field: value}, request_id=f"request-{index:03d}")
                self.assertFalse(result["saved"])
                self.assertEqual(result["status"], "INVALID_ASSET_METADATA")
        self.assertEqual(self.manager.list_assets()["assets"], [])

    def test_concurrent_asset_records_preserve_all_lineage_entries(self):
        def record(index):
            return self._record(
                request_id=f"concurrent-{index:03d}",
                filename=f"frame-{index:03d}.png",
                sha256=f"{index:064x}",
            )

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(record, range(50)))
        self.assertTrue(all(item["saved"] for item in results))
        self.assertEqual(len(self.manager.list_assets()["assets"]), 50)

    def test_corrupt_metadata_store_fails_closed_without_overwriting(self):
        self.manager.path.write_text("{broken", encoding="utf-8")
        result = self._record()
        self.assertEqual(result["status"], "ASSET_METADATA_STORE_INVALID")
        self.assertEqual(self.manager.path.read_text(encoding="utf-8"), "{broken")

    def test_lookup_rejects_invalid_asset_id(self):
        self.assertEqual(self.manager.get_asset("../bad")["status"], "INVALID_ASSET_ID")


if __name__ == "__main__":
    unittest.main()
