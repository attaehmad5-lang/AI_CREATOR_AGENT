"""Provider resource deletion adapter contract tests."""
import unittest

from projects.provider_store.provider_resource_deletion_adapter import (
    ProviderResourceDeletionAdapter,
)


class ProviderResourceDeletionAdapterTests(unittest.TestCase):
    def test_dispatches_only_registered_provider_and_forwards_idempotency_context(self):
        calls = []
        adapter = ProviderResourceDeletionAdapter({
            "image": lambda **kwargs: calls.append(kwargs) or {"deleted": True},
        })
        result = adapter.delete_resource(
            provider_id="image", resource_ref="remote-asset-1",
            tenant_id="tenant-a", idempotency_key="retention-delete:resource-1",
        )
        self.assertEqual(result, {"deleted": True, "status": "PROVIDER_RESOURCE_DELETED"})
        self.assertEqual(calls, [{
            "resource_ref": "remote-asset-1", "tenant_id": "tenant-a",
            "idempotency_key": "retention-delete:resource-1",
        }])

    def test_unknown_provider_fails_closed_without_calling_any_handler(self):
        calls = []
        adapter = ProviderResourceDeletionAdapter({
            "image": lambda **kwargs: calls.append(kwargs) or True,
        })
        result = adapter.delete_resource(
            provider_id="video", resource_ref="remote-asset-1",
            tenant_id="tenant-a", idempotency_key="retention-delete:resource-1",
        )
        self.assertEqual(result["status"], "PROVIDER_DELETION_UNSUPPORTED")
        self.assertFalse(result["deleted"])
        self.assertEqual(calls, [])

    def test_handler_exception_is_sanitized_and_remains_unconfirmed(self):
        def fail(**kwargs):
            raise RuntimeError("sensitive provider response must not escape")
        adapter = ProviderResourceDeletionAdapter({"image": fail})
        result = adapter.delete_resource(
            provider_id="image", resource_ref="remote-asset-1",
            tenant_id="tenant-a", idempotency_key="retention-delete:resource-1",
        )
        self.assertEqual(result["status"], "PROVIDER_DELETION_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("sensitive", str(result))

    def test_only_explicit_boolean_confirmation_counts_as_deleted(self):
        adapter = ProviderResourceDeletionAdapter({
            "image": lambda **kwargs: {"deleted": 1},
        })
        result = adapter.delete_resource(
            provider_id="image", resource_ref="remote-asset-1",
            tenant_id="tenant-a", idempotency_key="retention-delete:resource-1",
        )
        self.assertFalse(result["deleted"])
        self.assertEqual(result["status"], "PROVIDER_DELETION_UNCONFIRMED")

    def test_invalid_context_and_idempotency_key_are_rejected(self):
        calls = []
        adapter = ProviderResourceDeletionAdapter({
            "image": lambda **kwargs: calls.append(kwargs) or True,
        })
        result = adapter.delete_resource(
            provider_id="image", resource_ref="bad/ref", tenant_id="tenant-a",
            idempotency_key="retention-delete:resource-1",
        )
        self.assertEqual(result["status"], "INVALID_PROVIDER_RESOURCE_CONTEXT")
        result = adapter.delete_resource(
            provider_id="image", resource_ref="remote-asset-1", tenant_id="tenant-a",
            idempotency_key="wrong-key",
        )
        self.assertEqual(result["status"], "INVALID_DELETION_IDEMPOTENCY_KEY")
        self.assertEqual(calls, [])

    def test_invalid_handler_registry_is_rejected(self):
        with self.assertRaises(ValueError):
            ProviderResourceDeletionAdapter({"bad/id": lambda **kwargs: True})
        with self.assertRaises(ValueError):
            ProviderResourceDeletionAdapter({"image": "not-callable"})


if __name__ == "__main__":
    unittest.main()
