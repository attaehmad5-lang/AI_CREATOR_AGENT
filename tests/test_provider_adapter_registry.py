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
            model_id="image-v1", max_chunks=0,
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
        self.assertEqual(session.finish()["status"], "STREAM_NOT_OPEN")


    def test_execute_stream_dispatches_and_assembles_ordered_vendor_chunks(self):
        self.registry.register(
            "test-image",
            lambda **kwargs: iter((b"frame-1", "frame-2")),
            modalities=("image",), model_ids=("image-v1",),
            streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        result = self.registry.execute_stream(
            "test-image", modality="image", model_id="image-v1",
            request_id="stream-dispatch-1",
        )
        self.assertEqual(result["status"], "STREAM_EXECUTED")
        self.assertTrue(result["executed"])
        self.assertEqual(result["result"]["content"], b"frame-1frame-2")
        self.assertEqual(result["result"]["chunk_count"], 2)
        self.assertEqual(len(result["result"]["sha256"]), 64)

    def test_execute_stream_rejects_unverified_or_non_streaming_adapter_before_call(self):
        called = []
        self.registry.register(
            "test-image", lambda **kwargs: called.append(True) or iter((b"x",)),
            modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        denied = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-denied",
        )
        self.assertEqual(denied["status"], "STREAMING_UNSUPPORTED")
        self.assertFalse(denied["executed"])
        self.assertEqual(called, [])

    def test_execute_stream_handles_adapter_removed_between_session_open_and_dispatch(self):
        called = []
        self.registry.register(
            "test-image",
            lambda **kwargs: called.append(True) or iter((b"x",)),
            modalities=("image",), streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        original_resolve = self.registry.resolve_streaming
        resolutions = []

        def remove_after_open(provider_id, *, modality, model_id=None):
            resolutions.append(True)
            if len(resolutions) == 2:
                self.registry.unregister(provider_id)
            return original_resolve(
                provider_id, modality=modality, model_id=model_id
            )

        self.registry.resolve_streaming = remove_after_open
        result = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-race",
        )
        self.assertEqual(result["status"], "STREAMING_ADAPTER_UNAVAILABLE")
        self.assertFalse(result["started"])
        self.assertTrue(result["reservations_retained"])
        self.assertEqual(called, [])

    def test_execute_stream_retains_reservations_for_ambiguous_adapter_failure(self):
        self.registry.register(
            "test-image",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("secret vendor detail")),
            modalities=("image",), streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        result = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-error",
        )
        self.assertEqual(result["status"], "ADAPTER_STREAM_OUTCOME_UNKNOWN")
        self.assertTrue(result["reservations_retained"])
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("secret vendor detail", json.dumps(result))


    def test_execute_stream_enforces_model_allowlist_before_vendor_call(self):
        calls = []
        self.registry.register(
            "test-image",
            lambda **kwargs: calls.append(kwargs) or iter((b"must-not-run",)),
            modalities=("image",), model_ids=("image-v1",),
            streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        denied = self.registry.execute_stream(
            "test-image", modality="image", model_id="unlisted-model",
            request_id="stream-model-denied",
        )
        self.assertEqual(denied["status"], "MODEL_UNSUPPORTED")
        self.assertFalse(denied["started"])
        self.assertEqual(calls, [])

    def test_execute_stream_generator_failure_after_partial_output_is_ambiguous(self):
        def broken_stream(**kwargs):
            yield b"partial-private-payload"
            raise ConnectionError("vendor connection dropped")

        self.registry.register(
            "test-image", broken_stream, modalities=("image",),
            streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        result = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-midflight-failure",
        )
        self.assertEqual(result["status"], "ADAPTER_STREAM_OUTCOME_UNKNOWN")
        self.assertTrue(result["started"])
        self.assertTrue(result["reservations_retained"])
        self.assertEqual(result["error_type"], "ConnectionError")
        self.assertNotIn("partial-private-payload", json.dumps(result))
        self.assertNotIn("vendor connection dropped", json.dumps(result))

    def test_execute_stream_runs_incremental_chunk_validator_before_commit(self):
        seen = []
        self.registry.register(
            "test-image", lambda **kwargs: iter((b"safe-prefix", b"unsafe-tail")),
            modalities=("image",), streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )

        def validate(modality, sequence, payload):
            seen.append((modality, sequence, payload))
            return sequence == 0

        result = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-chunk-validation",
            chunk_validator=validate,
        )
        self.assertEqual(result["status"], "STREAM_CHUNK_VALIDATION_FAILED")
        self.assertTrue(result["started"])
        self.assertFalse(result["outcome_known"])
        self.assertTrue(result["reservations_retained"])
        self.assertEqual(seen, [
            ("image", 0, b"safe-prefix"),
            ("image", 1, b"unsafe-tail"),
        ])

    def test_execute_stream_validates_utf8_encoding_before_callback_and_sanitizes_surrogates(self):
        self.registry.register(
            "test-image", lambda **kwargs: iter(("valid-\u2603", "\ud800")),
            modalities=("image",), streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        seen = []
        result = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-surrogate-validator",
            chunk_validator=lambda modality, sequence, payload: seen.append(
                (modality, sequence, payload)
            ) or True,
        )
        self.assertEqual(result["status"], "STREAM_CHUNK_VALIDATION_ERROR")
        self.assertEqual(seen, [("image", 0, "valid-\u2603".encode("utf-8"))])
        self.assertEqual(result["error_type"], "UnicodeEncodeError")
        self.assertNotIn("\\ud800", json.dumps(result))

    def test_execute_stream_sanitizes_chunk_validator_exceptions_and_rejects_invalid_validator(self):
        self.registry.register(
            "test-image", lambda **kwargs: iter((b"payload",)),
            modalities=("image",), streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        invalid = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-invalid-validator",
            chunk_validator="not-callable",
        )
        self.assertEqual(invalid["status"], "INVALID_CHUNK_VALIDATOR")
        self.assertFalse(invalid["started"])

        def broken_validator(modality, sequence, payload):
            raise RuntimeError("private validator detail")

        failed = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-validator-error",
            chunk_validator=broken_validator,
        )
        self.assertEqual(failed["status"], "STREAM_CHUNK_VALIDATION_ERROR")
        self.assertTrue(failed["started"])
        self.assertTrue(failed["reservations_retained"])
        self.assertEqual(failed["error_type"], "RuntimeError")
        self.assertNotIn("private validator detail", json.dumps(failed))

    def test_execute_stream_closes_vendor_iterator_after_local_chunk_rejection(self):
        closed = []

        class ClosableChunks:
            def __iter__(self):
                yield b"first"
                yield b"second"

            def close(self):
                closed.append(True)

        self.registry.register(
            "test-image", lambda **kwargs: ClosableChunks(),
            modalities=("image",), streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        result = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-close-on-reject",
            chunk_validator=lambda modality, sequence, payload: sequence == 0,
        )
        self.assertEqual(result["status"], "STREAM_CHUNK_VALIDATION_FAILED")
        self.assertEqual(closed, [True])

    def test_execute_stream_enforces_session_limits_and_invalid_adapter_shape(self):
        self.registry.register(
            "test-image", lambda **kwargs: iter((b"123", b"456")),
            modalities=("image",), streaming_modalities=("image",),
        )
        self.manager.verify_connectivity(
            "test-image", lambda provider, **kwargs: {"ok": True}
        )
        limited = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-limit",
            max_bytes=4,
        )
        self.assertEqual(limited["status"], "BYTE_LIMIT_EXCEEDED")
        self.assertTrue(limited["reservations_retained"])

        self.registry.register(
            "test-image", lambda **kwargs: {"not": "an iterator"},
            modalities=("image",), streaming_modalities=("image",), replace=True,
        )
        invalid = self.registry.execute_stream(
            "test-image", modality="image", request_id="stream-shape",
        )
        self.assertEqual(invalid["status"], "ADAPTER_STREAM_INVALID")
        self.assertTrue(invalid["reservations_retained"])

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
