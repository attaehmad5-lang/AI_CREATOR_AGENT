import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import HTTPError

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_metadata_manager import ProviderMetadataManager


class _Response:
    def __init__(self, body=b'[{"id":"model-a","pipeline_tag":"text-to-image"}]', status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self.body[:size] if size >= 0 else self.body

    def close(self):
        self.closed = True


class _Opener:
    def __init__(self, response=None, error=None):
        self.response = response or _Response()
        self.error = error
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class ProviderMetadataManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / "catalog.json"
        self.state_path = self.root / "state.json"
        self.catalog_path.write_text(json.dumps({
            "version": "3.0",
            "providers": [{
                "id": "metadata_image",
                "name": "Metadata Image",
                "category": "image",
                "auth_type": "NONE",
                "connection_type": "API",
                "enabled": True,
                "subscription_required": False,
                "allowed_hosts": ["api.example.com"],
                "official_api_url": "https://docs.example.com/api",
                "capabilities": ["image_generation"],
                "status": "READY",
                "metadata_endpoint": "https://api.example.com/models",
                "metadata_query": {"limit": 100, "sort": "downloads"},
            }],
        }), encoding="utf-8")
        self.state_path.write_text(json.dumps({
            "version": "1.0", "policy": "USER_CONTROLLED", "providers": {},
        }), encoding="utf-8")
        self.manager = ConnectionManager(self.catalog_path, self.state_path)
        self.now = [datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)]
        self.metadata = ProviderMetadataManager(
            self.manager, clock=lambda: self.now[0], max_response_bytes=4096, max_models=20,
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _payload():
        return [
            {
                "id": "org/model-z",
                "pipeline_tag": "text-to-image",
                "version": "v2",
                "pricing": {"per_image": 0.002},
            },
            {
                "id": "org/model-a",
                "capabilities": ["image_generation", "image_editing"],
                "pricing": {"per_image": 0.004},
            },
        ]

    def test_refresh_persists_normalized_metadata_and_revision(self):
        result = self.metadata.refresh(
            "metadata_image", lambda endpoint, query, timeout: self._payload()
        )
        self.assertEqual(result["status"], "METADATA_REFRESHED")
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["model_count"], 2)
        provider = self.manager.get_provider("metadata_image")
        self.assertEqual([m["id"] for m in provider["models"]], ["org/model-a", "org/model-z"])
        self.assertEqual(provider["capabilities"], ["image_editing", "image_generation"])
        self.assertEqual(provider["metadata_status"], "VERIFIED")
        self.assertEqual(provider["metadata_source"], "https://api.example.com/models")
        self.assertEqual(provider["metadata_observed_at"], "2026-10-14T12:00:00+00:00")
        self.assertRegex(provider["metadata_revision"], r"^[0-9a-f]{64}$")

    def test_refresh_passes_only_configured_endpoint_query_and_bounded_timeout(self):
        seen = []
        result = self.metadata.refresh(
            "metadata_image",
            lambda endpoint, query, timeout: seen.append((endpoint, query, timeout)) or self._payload(),
            timeout_seconds=3.5,
        )
        self.assertTrue(result["refreshed"])
        self.assertEqual(seen, [(
            "https://api.example.com/models",
            {"limit": "100", "sort": "downloads"},
            3.5,
        )])

    def test_refresh_does_not_return_or_persist_raw_response_fields(self):
        raw = [{"id": "model-safe", "pipeline_tag": "text-to-image", "private_note": "token=never-store"}]
        result = self.metadata.refresh("metadata_image", lambda *_: raw)
        self.assertTrue(result["refreshed"])
        provider = self.manager.get_provider("metadata_image")
        self.assertNotIn("private_note", repr(provider))
        self.assertNotIn("never-store", repr(provider))
        self.assertNotIn("never-store", repr(result))

    def test_refresh_rejects_missing_provider(self):
        result = self.metadata.refresh("missing", lambda *_: self._payload())
        self.assertEqual(result["status"], "NOT_FOUND")
        self.assertFalse(result["refreshed"])

    def test_refresh_requires_configured_metadata_endpoint(self):
        provider = json.loads(self.catalog_path.read_text(encoding="utf-8"))["providers"][0]
        provider.pop("metadata_endpoint")
        provider.pop("metadata_query")
        self.catalog_path.write_text(json.dumps({"version": "3.0", "providers": [provider]}), encoding="utf-8")
        self.manager = ConnectionManager(self.catalog_path, self.state_path)
        self.metadata = ProviderMetadataManager(self.manager, clock=lambda: self.now[0])
        result = self.metadata.refresh("metadata_image", lambda *_: self._payload())
        self.assertEqual(result["status"], "METADATA_SOURCE_NOT_CONFIGURED")

    def test_refresh_rejects_invalid_timeout_and_fetcher(self):
        for timeout in (0, -1, float("nan"), float("inf"), 121):
            with self.subTest(timeout=timeout):
                result = self.metadata.refresh("metadata_image", lambda *_: self._payload(), timeout_seconds=timeout)
                self.assertEqual(result["status"], "INVALID_TIMEOUT")
        self.assertEqual(
            self.metadata.refresh("metadata_image", None)["status"],
            "INVALID_FETCHER",
        )

    def test_fetcher_exception_is_redacted(self):
        def fail(*_):
            raise RuntimeError("private token=do-not-leak")
        result = self.metadata.refresh("metadata_image", fail)
        self.assertEqual(result["status"], "METADATA_FETCH_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("do-not-leak", repr(result))

    def test_empty_or_wrong_shaped_payload_is_rejected(self):
        for payload in (None, {}, {"data": []}, [], "not-json"):
            with self.subTest(payload=repr(payload)):
                result = self.metadata.refresh("metadata_image", lambda *_: payload)
                self.assertEqual(result["status"], "INVALID_METADATA_PAYLOAD")
                self.assertFalse(result["refreshed"])

    def test_model_limit_is_enforced_before_persistence(self):
        result = self.metadata.refresh(
            "metadata_image",
            lambda *_: [{"id": f"model-{i}"} for i in range(21)],
        )
        self.assertEqual(result["status"], "METADATA_MODEL_LIMIT_EXCEEDED")
        self.assertNotIn("models", self.manager.get_provider("metadata_image"))

    def test_duplicate_model_ids_are_rejected(self):
        payload = [{"id": "same"}, {"id": "same"}]
        result = self.metadata.refresh("metadata_image", lambda *_: payload)
        self.assertEqual(result["status"], "DUPLICATE_METADATA_MODEL_ID")

    def test_secret_like_model_ids_are_rejected(self):
        for model_id in ("token=secret", "api_key=private", "Bearer secret", "password=secret"):
            with self.subTest(model_id=model_id):
                result = self.metadata.refresh("metadata_image", lambda *_: [{"id": model_id}])
                self.assertEqual(result["status"], "INVALID_METADATA_MODEL_ID")

    def test_invalid_model_version_is_rejected(self):
        result = self.metadata.refresh(
            "metadata_image", lambda *_: [{"id": "model-a", "version": "Bearer secret"}]
        )
        self.assertEqual(result["status"], "INVALID_MODEL_VERSION")

    def test_unknown_pipeline_tags_do_not_invent_capabilities(self):
        result = self.metadata.refresh(
            "metadata_image", lambda *_: [{"id": "model-a", "pipeline_tag": "unknown-future-task"}]
        )
        self.assertTrue(result["refreshed"])
        provider = self.manager.get_provider("metadata_image")
        self.assertEqual(provider["capabilities"], ["image_generation"])

    def test_invalid_capability_shape_is_rejected(self):
        for capabilities in ("image_generation", [1], ["x" * 100], ["Bad Capability"]):
            with self.subTest(capabilities=capabilities):
                result = self.metadata.refresh(
                    "metadata_image", lambda *_: [{"id": "model-a", "capabilities": capabilities}]
                )
                expected = "INVALID_MODEL_CAPABILITIES"
                self.assertEqual(result["status"], expected)

    def test_invalid_pricing_is_rejected(self):
        for pricing in ([], {"per_image": -1}, {"per_image": float("nan")}, {"Bad Key": 1}, {"per_image": "free"}):
            with self.subTest(pricing=repr(pricing)):
                result = self.metadata.refresh(
                    "metadata_image", lambda *_: [{"id": "model-a", "pricing": pricing}]
                )
                self.assertEqual(result["status"], "INVALID_MODEL_PRICING")

    def test_metadata_clock_must_be_timezone_aware(self):
        self.metadata = ProviderMetadataManager(
            self.manager, clock=lambda: datetime(2026, 10, 14, 12, 0)
        )
        result = self.metadata.refresh("metadata_image", lambda *_: self._payload())
        self.assertEqual(result["status"], "INVALID_METADATA_TIMESTAMP")

    def test_metadata_update_preserves_catalog_status_and_provider_identity(self):
        before = json.loads(self.catalog_path.read_text(encoding="utf-8"))["providers"][0]
        result = self.metadata.refresh("metadata_image", lambda *_: self._payload())
        self.assertTrue(result["refreshed"])
        after = json.loads(self.catalog_path.read_text(encoding="utf-8"))["providers"][0]
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["name"], before["name"])
        self.assertEqual(after["id"], before["id"])

    def test_metadata_update_rejects_unknown_fields(self):
        result = self.manager.update_provider_metadata(
            "metadata_image", {"metadata_revision": "a" * 64, "api_key": "secret"}
        )
        self.assertEqual(result["status"], "INVALID_METADATA_UPDATE")
        self.assertFalse(result["saved"])

    def test_metadata_update_rejects_invalid_models_without_mutation(self):
        before = self.catalog_path.read_bytes()
        result = self.manager.update_provider_metadata(
            "metadata_image",
            {"models": [{"id": "token=secret"}]},
        )
        self.assertEqual(result["status"], "INVALID_PROVIDER_CATALOG")
        self.assertFalse(result["saved"])
        self.assertEqual(self.catalog_path.read_bytes(), before)

    def test_metadata_refresh_refuses_to_mutate_loaded_provider(self):
        class Adapter:
            def generate_image(self, prompt, output_path):
                return None
        self.manager.load_provider("metadata_image", lambda _: Adapter())
        result = self.metadata.refresh("metadata_image", lambda *_: self._payload())
        self.assertEqual(result["status"], "PROVIDER_ALREADY_LOADED")
        self.assertFalse(result["refreshed"])
        self.assertNotIn("models", self.manager.get_provider("metadata_image"))

    def test_persistence_failure_does_not_report_success(self):
        import unittest.mock
        with unittest.mock.patch.object(
            self.manager, "update_provider_metadata",
            return_value={"status": "METADATA_PERSISTENCE_FAILED", "saved": False},
        ):
            result = self.metadata.refresh("metadata_image", lambda *_: self._payload())
        self.assertEqual(result["status"], "METADATA_PERSISTENCE_FAILED")
        self.assertFalse(result["refreshed"])

    def test_refresh_http_success_uses_allowlisted_https_and_closes_response(self):
        response = _Response()
        opener = _Opener(response=response)
        result = self.metadata.refresh_http("metadata_image", timeout_seconds=2.5, opener=opener)
        self.assertTrue(result["refreshed"])
        request, timeout = opener.calls[0]
        self.assertTrue(request.full_url.startswith("https://api.example.com/models?"))
        self.assertEqual(timeout, 2.5)
        self.assertTrue(response.closed)

    def test_refresh_http_rejects_oversized_response(self):
        response = _Response(body=b"x" * 5000)
        opener = _Opener(response=response)
        manager = ProviderMetadataManager(
            self.manager, clock=lambda: self.now[0], max_response_bytes=1024,
        )
        result = manager.refresh_http("metadata_image", opener=opener)
        self.assertEqual(result["status"], "METADATA_FETCH_FAILED")
        self.assertFalse(result["refreshed"])
        self.assertTrue(response.closed)

    def test_refresh_http_rejects_invalid_json(self):
        response = _Response(body=b"{invalid")
        result = self.metadata.refresh_http("metadata_image", opener=_Opener(response=response))
        self.assertEqual(result["status"], "METADATA_FETCH_FAILED")
        self.assertEqual(result["error_type"], "JSONDecodeError")

    def test_refresh_http_rejects_redirects_without_following(self):
        error = HTTPError("https://api.example.com/models", 302, "Found", {}, None)
        opener = _Opener(error=error)
        result = self.metadata.refresh_http("metadata_image", opener=opener)
        self.assertEqual(result["status"], "METADATA_FETCH_FAILED")
        self.assertEqual(len(opener.calls), 1)

    def test_refresh_http_rejects_invalid_content_length(self):
        response = _Response(headers={"Content-Length": "invalid"})
        result = self.metadata.refresh_http("metadata_image", opener=_Opener(response=response))
        self.assertEqual(result["status"], "METADATA_FETCH_FAILED")
        self.assertTrue(response.closed)

    def test_catalog_schema_rejects_metadata_endpoint_outside_allowlist(self):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["providers"][0]["metadata_endpoint"] = "https://evil.example.com/models"
        result = ConnectionManager.validate_catalog_schema(catalog)
        self.assertFalse(result["valid"])
        self.assertTrue(any("metadata_endpoint host" in error for error in result["errors"]))

    def test_catalog_schema_rejects_sensitive_metadata_query_keys(self):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["providers"][0]["metadata_query"] = {"api_key": "secret"}
        result = ConnectionManager.validate_catalog_schema(catalog)
        self.assertFalse(result["valid"])
        self.assertTrue(any("metadata_query" in error for error in result["errors"]))

    def test_catalog_schema_rejects_invalid_metadata_revision(self):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["providers"][0]["metadata_revision"] = "not-a-sha"
        result = ConnectionManager.validate_catalog_schema(catalog)
        self.assertFalse(result["valid"])
        self.assertTrue(any("metadata_revision" in error for error in result["errors"]))

    def test_catalog_schema_rejects_unknown_model_fields(self):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["providers"][0]["models"] = [{"id": "model-a", "module": "os.system"}]
        result = ConnectionManager.validate_catalog_schema(catalog)
        self.assertFalse(result["valid"])
        self.assertTrue(any("unsupported fields" in error for error in result["errors"]))

    def test_constructor_rejects_invalid_size_limits(self):
        for kwargs in (
            {"max_response_bytes": 1},
            {"max_response_bytes": 20_000_001},
            {"max_models": 0},
            {"max_models": 5001},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    ProviderMetadataManager(self.manager, **kwargs)


if __name__ == "__main__":
    unittest.main()
