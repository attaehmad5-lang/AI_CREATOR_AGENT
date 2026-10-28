import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projects.provider_store.connection_manager import ConnectionManager


ROOT = Path(__file__).resolve().parents[1]
SOURCE_CATALOG = ROOT / "projects" / "provider_store" / "provider_catalog.json"


class ProviderMetadataUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = self.root / "catalog.json"
        self.state = self.root / "state.json"
        self.catalog.write_bytes(SOURCE_CATALOG.read_bytes())
        self.state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.manager = ConnectionManager(self.catalog, self.state)
        self.provider_id = self.manager.list_providers()[0]["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_updates_descriptive_metadata_atomically_and_persists(self):
        result = self.manager.update_provider_metadata(
            self.provider_id,
            {
                "description": "Approved metadata description",
                "docs_url": "https://docs.example.test/provider",
                "tags": ["approved", "image"],
            },
        )
        self.assertEqual(result["status"], "METADATA_UPDATED")
        self.assertTrue(result["saved"])
        self.assertEqual(result["updated_fields"], ["description", "docs_url", "tags"])

        reloaded = ConnectionManager(self.catalog, self.state)
        metadata = reloaded.get_provider_metadata(self.provider_id)
        self.assertEqual(metadata["description"], "Approved metadata description")
        self.assertEqual(metadata["tags"], ["approved", "image"])
        self.assertEqual(metadata["docs_url"], "https://docs.example.test/provider")

    def test_rejects_identity_capability_and_credential_fields(self):
        original = self.catalog.read_bytes()
        for patch_data, expected in (
            ({"id": "spoofed"}, "METADATA_FIELD_NOT_EDITABLE"),
            ({"supported_models": []}, "METADATA_FIELD_NOT_EDITABLE"),
            ({"api_key": "secret"}, "METADATA_FIELD_NOT_EDITABLE"),
            ({"description": "text", "credentials": "secret"}, "METADATA_FIELD_NOT_EDITABLE"),
        ):
            with self.subTest(patch_data=patch_data):
                result = self.manager.update_provider_metadata(self.provider_id, patch_data)
                self.assertEqual(result["status"], expected)
                self.assertEqual(self.catalog.read_bytes(), original)

    def test_rejects_invalid_tags_and_control_characters(self):
        for patch_data in (
            {"tags": ["duplicate", "DUPLICATE"]},
            {"tags": [""]},
            {"tags": ["x" * 65]},
            {"description": "bad\nvalue"},
            {"description": "x" * 2049},
        ):
            with self.subTest(patch_data=patch_data):
                self.assertEqual(
                    self.manager.update_provider_metadata(self.provider_id, patch_data)["status"],
                    "INVALID_METADATA",
                )

    def test_failed_atomic_write_does_not_mutate_memory_or_disk(self):
        original_bytes = self.catalog.read_bytes()
        original_memory = json.loads(json.dumps(self.manager.catalog))
        with patch.object(
            self.manager,
            "_atomic_write_json",
            side_effect=OSError("simulated disk failure"),
        ):
            result = self.manager.update_provider_metadata(
                self.provider_id, {"description": "must not persist"}
            )
        self.assertEqual(result["status"], "METADATA_WRITE_FAILED")
        self.assertFalse(result["saved"])
        self.assertEqual(self.catalog.read_bytes(), original_bytes)
        self.assertEqual(self.manager.catalog, original_memory)

    def test_missing_provider_and_empty_patch_fail_closed(self):
        self.assertEqual(
            self.manager.update_provider_metadata("missing", {"description": "x"})["status"],
            "NOT_FOUND",
        )
        self.assertEqual(
            self.manager.update_provider_metadata(self.provider_id, {})["status"],
            "INVALID_METADATA",
        )


if __name__ == "__main__":
    unittest.main()
