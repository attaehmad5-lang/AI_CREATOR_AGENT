import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager


class ProviderCatalogValidationTests(unittest.TestCase):
    def test_primary_and_master_provider_catalogs_are_synchronized(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        primary = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        master = json.loads((root / "provider_catalog_master.json").read_text(encoding="utf-8"))
        self.assertEqual(primary.get("version"), master.get("version"))
        self.assertEqual(primary.get("providers"), master.get("providers"))

    def test_catalog_rejects_paid_subscription_marked_as_zero_cost(self):
        result = ConnectionManager.validate_catalog_schema({
            "version": "3.0",
            "providers": [{
                "id": "contradictory_cost", "name": "Contradictory Cost",
                "category": "image", "auth_type": "API_KEY",
                "connection_type": "API", "enabled": False,
                "allowed_hosts": ["api.example.com"],
                "subscription_required": True, "zero_cost_allowed": True,
            }],
        })
        self.assertFalse(result["valid"])
        self.assertTrue(any("cannot require a subscription" in error for error in result["errors"]))

    def test_catalog_rejects_unsafe_provider_links(self):
        for url in (
            "javascript:alert(1)",
            "http://provider.example.com",
            "https://user:password@provider.example.com/docs",
            "https://provider.example.com:99999/docs",
            "https://provider.example.com/\nmalicious",
        ):
            with self.subTest(url=url):
                result = ConnectionManager.validate_catalog_schema({
                    "version": "3.0",
                    "providers": [{
                        "id": "provider", "name": "Provider",
                        "category": "image", "auth_type": "NONE",
                        "connection_type": "API", "enabled": False,
                        "subscription_required": False,
                        "official_api_url": url,
                    }],
                })
                self.assertFalse(result["valid"])
                self.assertTrue(any("safe HTTPS URL" in error for error in result["errors"]))

    def test_catalog_rejects_unsafe_provider_ids(self):
        for provider_id in ("../escape", "provider name", "provider/id", "x" * 101):
            with self.subTest(provider_id=provider_id):
                result = ConnectionManager.validate_catalog_schema({
                    "version": "3.0",
                    "providers": [{
                        "id": provider_id, "name": "Provider",
                        "category": "image", "auth_type": "NONE",
                        "connection_type": "API", "enabled": False,
                        "subscription_required": False,
                    }],
                })
                self.assertFalse(result["valid"])
                self.assertTrue(any("id contains invalid characters" in error for error in result["errors"]))

    def test_catalog_rejects_malformed_versions_instead_of_downgrading_schema(self):
        for version in ("3.invalid", "", True, None, {"major": 3}):
            with self.subTest(version=version):
                result = ConnectionManager.validate_catalog_schema({
                    "version": version,
                    "providers": [{
                        "id": "provider", "name": "Provider",
                        "category": "image", "auth_type": "NONE",
                        "connection_type": "API", "enabled": False,
                        "subscription_required": False,
                    }],
                })
                self.assertFalse(result["valid"])
                self.assertTrue(any("catalog.version" in error for error in result["errors"]))

    def test_v3_catalog_requires_complete_provider_identity_and_billing_flags(self):
        valid_provider = {
            "id": "complete", "name": "Complete Provider",
            "category": "image", "auth_type": "NONE",
            "connection_type": "API", "enabled": False,
            "subscription_required": False,
        }
        self.assertTrue(ConnectionManager.validate_catalog_schema({
            "version": "3.0", "providers": [valid_provider],
        })["valid"])
        for field in ("name", "category", "auth_type", "connection_type", "enabled", "subscription_required"):
            with self.subTest(field=field):
                incomplete = dict(valid_provider)
                incomplete.pop(field)
                result = ConnectionManager.validate_catalog_schema({
                    "version": "3.0", "providers": [incomplete],
                })
                self.assertFalse(result["valid"])
                self.assertTrue(any(field in error and "required" in error for error in result["errors"]))

    def test_schema_rejects_enabled_api_provider_without_allowlist(self):
        result = ConnectionManager.validate_catalog_schema({
            "version": "3.0",
            "providers": [{
                "id": "unsafe_api", "name": "Unsafe API",
                "category": "image", "auth_type": "NONE",
                "connection_type": "API", "enabled": True,
            }]
        })
        self.assertFalse(result["valid"])
        self.assertTrue(any("allowed_hosts is required" in error for error in result["errors"]))

    def test_enabled_api_providers_have_explicit_host_allowlists(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        for provider in catalog["providers"]:
            if provider.get("enabled") and str(provider.get("connection_type", "")).upper() == "API":
                hosts = provider.get("allowed_hosts")
                self.assertIsInstance(hosts, list, provider.get("id"))
                self.assertTrue(hosts, provider.get("id"))
                self.assertTrue(
                    all(isinstance(host, str) and host.strip() == host for host in hosts),
                    provider.get("id"),
                )

    def test_deprecated_provider_requires_reason_date_and_stays_disabled(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        provider = next(item for item in catalog["providers"] if item["id"] == "openai_sora")
        self.assertTrue(provider["deprecated"])
        self.assertFalse(provider["enabled"])
        self.assertEqual(provider["deprecation_date"], "2026-09-24")
        self.assertTrue(provider["deprecation_reason"].strip())
        self.assertTrue(ConnectionManager.validate_catalog_schema(catalog)["valid"])

        enabled_catalog = json.loads(json.dumps(catalog))
        next(item for item in enabled_catalog["providers"] if item["id"] == "openai_sora")["enabled"] = True
        result = ConnectionManager.validate_catalog_schema(enabled_catalog)
        self.assertFalse(result["valid"])
        self.assertTrue(any("deprecated provider must remain disabled" in error for error in result["errors"]))

        missing_reason = json.loads(json.dumps(catalog))
        next(item for item in missing_reason["providers"] if item["id"] == "openai_sora").pop("deprecation_reason")
        result = ConnectionManager.validate_catalog_schema(missing_reason)
        self.assertFalse(result["valid"])
        self.assertTrue(any("deprecation_reason is required" in error for error in result["errors"]))

        invalid_date = json.loads(json.dumps(catalog))
        next(item for item in invalid_date["providers"] if item["id"] == "openai_sora")["deprecation_date"] = "2026-02-30"
        result = ConnectionManager.validate_catalog_schema(invalid_date)
        self.assertFalse(result["valid"])
        self.assertTrue(any("deprecation_date must be an ISO" in error for error in result["errors"]))

        control_character = json.loads(json.dumps(catalog))
        next(item for item in control_character["providers"] if item["id"] == "openai_sora")["deprecation_reason"] = "unsafe\\nreason"
        result = ConnectionManager.validate_catalog_schema(control_character)
        self.assertFalse(result["valid"])
        self.assertTrue(any("deprecation_reason is required" in error for error in result["errors"]))

    def test_runtime_enablement_cannot_reactivate_deprecated_provider(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            catalog_path, state_path = temp / "catalog.json", temp / "state.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
            manager = ConnectionManager(catalog_path, state_path)
            result = manager.set_provider_enabled("openai_sora", True)
            self.assertFalse(result["saved"])
            self.assertEqual(result["status"], "INVALID_PROVIDER_CATALOG")
            self.assertFalse(manager.get_provider("openai_sora")["enabled"])

    def test_runtime_enablement_requires_official_docs_and_capabilities(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        provider = next(item for item in catalog["providers"] if item["id"] == "pollinations")
        provider["enabled"] = False
        provider.pop("official_api_url", None)
        provider.pop("capabilities", None)
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            catalog_path, state_path = temp / "catalog.json", temp / "state.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
            manager = ConnectionManager(catalog_path, state_path)
            result = manager.set_provider_enabled("pollinations", True)
            self.assertFalse(result["saved"])
            self.assertEqual(result["status"], "INVALID_PROVIDER_CATALOG")
            self.assertFalse(manager.get_provider("pollinations")["enabled"])
            self.assertTrue(any("official_api_url is required" in error for error in result["errors"]))
            self.assertTrue(any("capabilities must be a non-empty list" in error for error in result["errors"]))

    def test_huggingface_account_auth_host_is_allowlisted(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        provider = next(item for item in catalog["providers"] if item["id"] == "huggingface")
        self.assertIn("huggingface.co", provider["allowed_hosts"])
        endpoint = ConnectionManager.validate_endpoint(
            "https://huggingface.co/api/whoami-v2",
            allowed_hosts=set(provider["allowed_hosts"]),
        )
        self.assertTrue(endpoint["valid"], endpoint)

    def test_pollinations_uses_current_documented_api_auth_and_host(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        provider = next(item for item in catalog["providers"] if item["id"] == "pollinations")
        self.assertEqual(provider["auth_type"], "API_KEY")
        self.assertEqual(provider["token_env"], "POLLINATIONS_API_KEY")
        self.assertIn("gen.pollinations.ai", provider["allowed_hosts"])
        self.assertIn("enter.pollinations.ai", provider["allowed_hosts"])
        self.assertEqual(provider["official_api_url"], "https://gen.pollinations.ai/docs")
        self.assertIsNot(provider.get("zero_cost_allowed"), True)
        self.assertTrue(ConnectionManager.validate_catalog_schema(catalog)["valid"])

    def test_enabled_providers_have_explicit_generation_capabilities(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        enabled = [provider for provider in catalog["providers"] if provider.get("enabled") is True]
        self.assertTrue(enabled)
        for provider in enabled:
            with self.subTest(provider=provider["id"]):
                capabilities = provider.get("capabilities")
                self.assertIsInstance(capabilities, list)
                self.assertTrue(capabilities)
                self.assertTrue(provider.get("official_api_url", "").startswith("https://"))
                self.assertEqual(len(capabilities), len(set(capabilities)))
                self.assertTrue(all(
                    isinstance(item, str) and item == item.casefold() and item.strip()
                    for item in capabilities
                ))
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            catalog_path, state_path = temp / "catalog.json", temp / "state.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
            manager = ConnectionManager(catalog_path, state_path)
            matches = manager.discover_providers(
                capability="image_generation", enabled_only=True
            )
            self.assertEqual({provider["id"] for provider in matches}, {"huggingface", "pollinations"})

    def test_every_catalog_modality_has_a_callable_contract(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalog = json.loads((root / "provider_catalog.json").read_text(encoding="utf-8"))
        required_methods = (
            "generate_image", "generate_video", "generate_speech", "generate_audio",
            "generate_music", "generate_text", "transcribe", "translate", "dub",
            "edit_image", "upscale_image", "upscale_video", "generate_lipsync", "edit_video",
        )
        adapter = type("CompleteTestAdapter", (), {
            method: (lambda self, *args, **kwargs: None) for method in required_methods
        })()
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            catalog_path = temp / "catalog.json"
            state_path = temp / "state.json"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
            state_path.write_text(json.dumps({
                "version": "1.0", "policy": "USER_CONTROLLED", "providers": {},
            }), encoding="utf-8")
            manager = ConnectionManager(catalog_path, state_path)
            for provider in catalog["providers"]:
                with self.subTest(provider=provider["id"], category=provider["category"]):
                    result = manager.validate_provider_contract(provider["id"], adapter)
                    self.assertTrue(result["valid"], result)

    def test_supported_model_metadata_requires_unique_safe_ids_and_versions(self):
        provider = {
            "id": "model_provider", "name": "Model Provider",
            "category": "image", "auth_type": "NONE",
            "connection_type": "API", "enabled": False,
            "subscription_required": False,
            "supported_models": [{"id": "publisher/model-v1", "versions": ["1.0", "1.1"]}],
        }
        valid = ConnectionManager.validate_catalog_schema({
            "version": "3.0", "providers": [provider],
        })
        self.assertTrue(valid["valid"], valid["errors"])
        invalid_records = (
            [{"id": "../unsafe", "versions": ["1.0"]}],
            [{"id": "model", "versions": ["1.0", "1.0"]}],
            [{"id": "model"}, {"id": "MODEL"}],
            ["model"],
        )
        for records in invalid_records:
            with self.subTest(records=records):
                candidate = json.loads(json.dumps(provider))
                candidate["supported_models"] = records
                result = ConnectionManager.validate_catalog_schema({
                    "version": "3.0", "providers": [candidate],
                })
                self.assertFalse(result["valid"])

    def test_capability_identifiers_must_be_bounded_unique_and_normalized(self):
        base = {
            "version": "3.0",
            "providers": [{
                "id": "capability_test", "name": "Capability Test",
                "category": "image", "auth_type": "NONE",
                "connection_type": "API", "enabled": False,
                "subscription_required": False,
            }],
        }
        for capabilities in (
            [""],
            ["Image-Generation"],
            ["image_generation", "image_generation"],
            ["image_generation", "IMAGE_GENERATION"],
            ["x" * 81],
            ["capability_" + str(index) for index in range(101)],
        ):
            with self.subTest(capabilities=capabilities):
                candidate = json.loads(json.dumps(base))
                candidate["providers"][0]["capabilities"] = capabilities
                result = ConnectionManager.validate_catalog_schema(candidate)
                self.assertFalse(result["valid"])
                self.assertTrue(any("capabilities must contain unique" in error for error in result["errors"]))

        valid = json.loads(json.dumps(base))
        valid["providers"][0]["capabilities"] = ["image_generation", "inpainting_v2"]
        self.assertTrue(ConnectionManager.validate_catalog_schema(valid)["valid"])

    def test_all_provider_catalog_json_files_pass_schema_validation(self):
        root = Path(__file__).resolve().parents[1] / "projects" / "provider_store"
        catalogs = []
        for path in sorted(root.rglob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("providers"), list):
                catalogs.append(path)
                result = ConnectionManager.validate_catalog_schema(data)
                self.assertTrue(result["valid"], f"{path.name}: {result['errors']}")
        self.assertTrue(catalogs, "No provider catalog JSON files were found")


if __name__ == "__main__":
    unittest.main()
