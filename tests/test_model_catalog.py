import unittest

from projects.provider_store.model_catalog import ModelCatalog


class FakeConnectionManager:
    def __init__(self, providers):
        self.providers = providers

    def list_providers(self):
        return [dict(provider) for provider in self.providers]


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeConnectionManager([
            {
                "id": "provider-a", "name": "Provider A", "category": "image",
                "enabled": True,
                "supported_models": [
                    {"id": "img-z", "name": "Z Image", "capabilities": ["image", "upscale"]},
                    {"id": "img-a", "display_name": "A Image", "capabilities": ["image"]},
                    {"id": "secret-model", "api_key": "never-expose"},
                    {"id": "../bad", "name": "Invalid ID"},
                ],
            },
            {
                "id": "provider-b", "name": "Provider B", "category": "video",
                "enabled": False,
                "supported_models": [
                    {"id": "vid-1", "name": "Video One", "capabilities": ["video"]},
                ],
            },
        ])
        self.catalog = ModelCatalog(self.manager)

    def test_lists_models_in_deterministic_order_and_filters_disabled(self):
        result = self.catalog.list_models()
        self.assertEqual(result["status"], "MODELS_AVAILABLE")
        self.assertEqual([item["id"] for item in result["models"]], ["img-a", "img-z"])
        self.assertEqual(result["source"], "catalog_metadata_only")
        self.assertNotIn("api_key", repr(result))
        self.assertNotIn("secret-model", repr(result))

    def test_can_include_disabled_providers_and_filter_capability(self):
        result = self.catalog.list_models(capability="VIDEO", include_disabled=True)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["models"][0]["id"], "vid-1")
        self.assertFalse(result["models"][0]["provider_enabled"])

    def test_exact_case_insensitive_model_lookup(self):
        found = self.catalog.find_model("IMG-Z")
        self.assertEqual(found["status"], "MODEL_FOUND")
        self.assertEqual(found["matches"][0]["provider_id"], "provider-a")
        self.assertEqual(self.catalog.find_model("img")["status"], "MODEL_NOT_FOUND")

    def test_provider_filter_and_unknown_provider(self):
        result = self.catalog.list_models(provider_id="provider-a")
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            self.catalog.list_models(provider_id="missing")["status"],
            "PROVIDER_NOT_FOUND",
        )

    def test_invalid_arguments_fail_closed(self):
        self.assertEqual(self.catalog.list_models(capability="../bad")["status"], "INVALID_CAPABILITY")
        self.assertEqual(self.catalog.list_models(include_disabled="yes")["status"], "INVALID_INCLUDE_DISABLED")
        self.assertEqual(self.catalog.find_model("../bad")["status"], "INVALID_MODEL_ID")
        with self.assertRaises(ValueError):
            ModelCatalog(object())


if __name__ == "__main__":
    unittest.main()
