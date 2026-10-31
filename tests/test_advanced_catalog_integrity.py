import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.advanced.catalog_integrity import validate_advanced_catalogs

ROOT = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
ADV = ROOT / "advanced"


class AdvancedCatalogIntegrityTests(unittest.TestCase):
    def test_real_catalogs_are_consistent(self):
        result = validate_advanced_catalogs(
            ROOT / "provider_catalog.json",
            ADV / "advanced_provider_catalog.json",
            ADV / "advanced_categories.json",
        )
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["central_provider_count"], 54)
        self.assertEqual(result["central_advanced_provider_count"], 18)
        self.assertEqual(result["advanced_provider_count"], 18)
        self.assertEqual(result["category_count"], 6)

    def test_invalid_utf8_and_malformed_category_definitions_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            central = root / "central.json"
            advanced = root / "advanced.json"
            categories = root / "categories.json"
            central.write_bytes(b"\\xff")
            advanced.write_text(json.dumps({"providers": [], "policy": {
                "auto_purchase": False, "subscription_bypass": False,
                "api_key_logging": False}}), encoding="utf-8")
            categories.write_text(json.dumps({"categories": {
                "script_story": {"name": "", "capabilities": "not-a-list"}
            }}), encoding="utf-8")
            result = validate_advanced_catalogs(central, advanced, categories)

        self.assertFalse(result["valid"])
        self.assertTrue(any("central catalog unreadable" in error for error in result["errors"]))
        self.assertTrue(any("display name is missing" in error for error in result["errors"]))
        self.assertTrue(any("capabilities are invalid" in error for error in result["errors"]))

    def test_duplicate_ids_and_unsafe_policy_are_rejected(self):
        central = {"providers": [{"id": "a", "category": "advanced_audio"}]}
        advanced = {"policy": {"auto_purchase": True, "subscription_bypass": False, "api_key_logging": False},
                    "providers": [{"id": "a"}, {"id": "a"}]}
        categories = {"categories": {key: {} for key in (
            "script_story", "advanced_image_editing", "advanced_upscaling",
            "advanced_transcription", "advanced_audio", "advanced_video_editing")}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("central.json", "advanced.json", "categories.json")]
            for path, value in zip(paths, (central, advanced, categories)):
                path.write_text(json.dumps(value), encoding="utf-8")
            result = validate_advanced_catalogs(*paths)
        self.assertFalse(result["valid"])
        self.assertTrue(any("duplicate id" in error for error in result["errors"]))
        self.assertIn("policy auto_purchase must be false", result["errors"])


    def test_empty_and_duplicate_category_capabilities_are_rejected(self):
        for capabilities in ([], ["image", "IMAGE"]):
            with self.subTest(capabilities=capabilities):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    central = root / "central.json"
                    advanced = root / "advanced.json"
                    categories = root / "categories.json"
                    central.write_text(json.dumps({"providers": []}), encoding="utf-8")
                    advanced.write_text(json.dumps({"providers": [], "policy": {
                        "auto_purchase": False, "subscription_bypass": False,
                        "api_key_logging": False}}), encoding="utf-8")
                    categories.write_text(json.dumps({"categories": {
                        key: {"name": key, "capabilities": ["ok"]}
                        for key in (
                            "script_story", "advanced_image_editing", "advanced_upscaling",
                            "advanced_transcription", "advanced_audio", "advanced_video_editing"
                        )
                    }}), encoding="utf-8")
                    value = json.loads(categories.read_text(encoding="utf-8"))
                    value["categories"]["advanced_audio"]["capabilities"] = capabilities
                    categories.write_text(json.dumps(value), encoding="utf-8")
                    result = validate_advanced_catalogs(central, advanced, categories)
                self.assertFalse(result["valid"])
                self.assertTrue(any("capabilities are invalid" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
