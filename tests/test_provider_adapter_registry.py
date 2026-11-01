import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_adapter_registry import ProviderAdapterRegistry


class ProviderAdapterRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = self.root / "catalog.json"
        self.state = self.root / "state.json"
        self.catalog.write_text(json.dumps({"providers": [
            {
                "id": "test-image", "name": "Test Image", "category": "image",
                "auth_type": "NONE", "connection_type": "API", "enabled": True,
                "supported_models": [{"id": "image-v1"}],
            },
            {
                "id": "disabled-image", "name": "Disabled Image", "category": "image",
                "auth_type": "NONE", "connection_type": "API", "enabled": False,
                "supported_models": [{"id": "image-v1"}],
            },
        ]}), encoding="utf-8")
        self.state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.manager = ConnectionManager(self.catalog, self.state)
        self.registry = ProviderAdapterRegistry(self.manager)
        self.calls = []
        self.adapter = lambda **kwargs: self.calls.append(kwargs) or {
            "started": True, "success": True, "output_path": "artifact.png"
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_dispatch_requires_verified_enabled_provider_and_supported_contract(self):
        registered = self.registry.register(
            "test-image", self.adapter, modalities=("image",), model_ids=("image-v1",)
        )
        self.assertTrue(registered["registered"])
        self.assertEqual(
            self.registry.resolve("test-image", modality="image", model_id="image-v1")["status"],
            "PROVIDER_NOT_VERIFIED",
        )
        self.assertEqual(
            self.registry.execute("test-image", modality="image", request_id="req-1")["status"],
            "PROVIDER_NOT_VERIFIED",
        )
        verified = self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        self.assertTrue(verified["verified"])
        self.assertEqual(
            self.registry.resolve("test-image", modality="video")["status"],
            "MODALITY_UNSUPPORTED",
        )
        self.assertEqual(
            self.registry.resolve("test-image", modality="image", model_id="unknown")["status"],
            "MODEL_UNSUPPORTED",
        )
        result = self.registry.execute(
            "test-image", modality="image", model_id="image-v1", request_id="req-2", prompt="test"
        )
        self.assertEqual(result["status"], "ADAPTER_EXECUTED")
        self.assertTrue(result["executed"])
        self.assertEqual(self.calls[0]["provider_id"], "test-image")
        self.assertEqual(self.calls[0]["request_id"], "req-2")
        self.assertEqual(self.calls[0]["model_id"], "image-v1")
        self.assertEqual(self.calls[0]["prompt"], "test")

    def test_streaming_requires_explicit_registered_capability_and_verified_provider(self):
        registered = self.registry.register(
            "test-image", self.adapter, modalities=("image",),
            model_ids=("image-v1",), streaming_modalities=("image",),
        )
        self.assertTrue(registered["registered"])
        self.assertEqual(
            self.registry.resolve_streaming(
                "test-image", modality="image", model_id="image-v1"
            )["status"],
            "PROVIDER_NOT_VERIFIED",
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        resolved = self.registry.resolve_streaming(
            "test-image", modality="image", model_id="image-v1"
        )
        self.assertEqual(resolved["status"], "STREAMING_ADAPTER_RESOLVED")
        self.assertIs(resolved["adapter"], self.adapter)

    def test_open_stream_session_requires_verified_streaming_adapter_and_bounds(self):
        denied = self.registry.open_stream_session(
            "test-image", modality="image", request_id="stream-before-verify"
        )
        self.assertFalse(denied["opened"])
        self.assertEqual(denied["status"], "PROVIDER_NOT_VERIFIED")

        self.registry.register(
            "test-image", self.adapter, modalities=("image",),
            model_ids=("image-v1",), streaming_modalities=("image",),
        )
        unverified = self.registry.open_stream_session(
            "test-image", modality="image", request_id="stream-unverified",
            model_id="image-v1",
        )
        self.assertFalse(unverified["opened"])
        self.assertEqual(unverified["status"], "PROVIDER_NOT_VERIFIED")

        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        invalid = self.registry.open_stream_session(
            "test-image", modality="image", request_id="stream-invalid",
            max_chunks=0,
        )
        self.assertFalse(invalid["opened"])
        self.assertEqual(invalid["status"], "INVALID_STREAM_SESSION_OPTIONS")

        opened = self.registry.open_stream_session(
            "test-image", modality="image", request_id="stream-valid",
            model_id="image-v1", max_chunks=2, max_bytes=8,
        )
        self.assertTrue(opened["opened"])
        self.assertEqual(opened["status"], "STREAM_SESSION_OPENED")
        session = opened["session"]
        self.assertEqual(session.append(sequence=0, chunk=b"four")["status"], "CHUNK_ACCEPTED")
        self.assertEqual(session.append(sequence=1, chunk=b"five")["status"], "CHUNK_ACCEPTED")
        self.assertEqual(session.append(sequence=2, chunk=b"x")["status"], "CHUNK_LIMIT_EXCEEDED")
        self.assertEqual(session.finish()["status"], "STREAM_COMPLETED")

    def test_streaming_capability_must_be_subset_of_registered_modalities(self):
        result = self.registry.register(
            "test-image", self.adapter, modalities=("image",),
            streaming_modalities=("video",),
        )
        self.assertEqual(result["status"], "STREAMING_MODALITY_NOT_REGISTERED")
        self.assertFalse(result["registered"])
        invalid = self.registry.register(
            "test-image", self.adapter, modalities=("image",),
            streaming_modalities="image",
        )
        self.assertEqual(invalid["status"], "INVALID_STREAMING_MODALITIES")

    def test_disabled_provider_cannot_dispatch_even_if_adapter_is_registered(self):
        result = self.registry.register("disabled-image", self.adapter, modalities=("image",))
        self.assertTrue(result["registered"])
        self.manager.verify_connectivity(
            "disabled-image", lambda provider, **kwargs: {"ok": True}
        )
        denied = self.registry.execute(
            "disabled-image", modality="image", request_id="req-disabled"
        )
        self.assertEqual(denied["status"], "PROVIDER_DISABLED")
        self.assertEqual(self.calls, [])

    def test_duplicate_registration_and_reserved_options_are_rejected(self):
        self.assertTrue(self.registry.register("test-image", self.adapter, modalities=("image",))["registered"])
        self.assertEqual(
            self.registry.register("test-image", self.adapter, modalities=("image",))["status"],
            "ADAPTER_ALREADY_REGISTERED",
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        result = self.registry.execute(
            "test-image", modality="image", request_id="req-reserved", provider_id="spoofed"
        )
        self.assertEqual(result["status"], "RESERVED_ADAPTER_OPTION")
        self.assertEqual(self.calls, [])

    def test_adapter_exceptions_and_invalid_responses_are_sanitized(self):
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        self.registry.register(
            "test-image", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("secret-token")),
            modalities=("image",),
        )
        result = self.registry.execute("test-image", modality="image", request_id="req-error")
        self.assertEqual(result["status"], "ADAPTER_OUTCOME_UNKNOWN")
        self.assertFalse(result["outcome_known"])
        self.assertTrue(result["reservations_retained"])
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("secret-token", json.dumps(result))

        self.registry.register(
            "test-image", lambda **kwargs: {"unexpected": "shape"},
            modalities=("image",), replace=True,
        )
        invalid = self.registry.execute("test-image", modality="image", request_id="req-invalid")
        self.assertEqual(invalid["status"], "ADAPTER_RESPONSE_INVALID")
        self.assertNotIn("unexpected", json.dumps(invalid))

    def test_service_callable_fails_closed_before_dispatch_and_propagates_adapter_errors(self):
        self.registry.register("test-image", self.adapter, modalities=("image",))
        blocked = self.registry.as_callable("test-image", modality="image")
        not_verified = blocked(provider_id="test-image", request_id="req-before-verify")
        self.assertFalse(not_verified["started"])
        self.assertEqual(self.calls, [])

        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        success = blocked(provider_id="test-image", request_id="req-callable")
        self.assertTrue(success["started"])
        self.assertEqual(self.calls[0]["request_id"], "req-callable")

        self.registry.register(
            "test-image",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("ambiguous vendor failure")),
            modalities=("image",), replace=True,
        )
        risky = self.registry.as_callable("test-image", modality="image")
        with self.assertRaisesRegex(RuntimeError, "ambiguous vendor failure"):
            risky(provider_id="test-image", request_id="req-ambiguous")
        # The callable does not misrepresent an exception as a safe no-start outcome.

    def test_service_callable_uses_routed_model_and_enforces_allowlist(self):
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        self.registry.register(
            "test-image", self.adapter, modalities=("image",), model_ids=("image-v1",)
        )
        dispatch = self.registry.as_callable("test-image", modality="image")
        accepted = dispatch(
            provider_id="test-image", request_id="req-model-ok", model_id="image-v1"
        )
        self.assertTrue(accepted["started"])
        self.assertEqual(self.calls[0]["model_id"], "image-v1")

        rejected = dispatch(
            provider_id="test-image", request_id="req-model-denied", model_id="not-allowed"
        )
        self.assertFalse(rejected["started"])
        self.assertEqual(rejected["error_code"], "MODEL_UNSUPPORTED")
        self.assertEqual(len(self.calls), 1)

    def test_registration_validation_listing_and_unregistration(self):
        self.assertEqual(
            self.registry.register("missing", self.adapter, modalities=("image",))["status"],
            "PROVIDER_NOT_FOUND",
        )
        self.assertEqual(
            self.registry.register("test-image", object(), modalities=("image",))["status"],
            "INVALID_ADAPTER",
        )
        self.assertTrue(self.registry.register("test-image", self.adapter, modalities=("image",))["registered"])
        listed = self.registry.list_adapters()
        self.assertEqual(listed["count"], 1)
        self.assertNotIn("\"adapter\":", json.dumps(listed))
        self.assertTrue(self.registry.unregister("test-image")["unregistered"])
        self.assertEqual(self.registry.list_adapters()["count"], 0)


if __name__ == "__main__":
    unittest.main()
