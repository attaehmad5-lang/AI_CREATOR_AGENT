import tempfile
import unittest
from pathlib import Path

from projects.provider_store.tenant_state_partition import TenantStatePartition


class TenantStatePartitionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "tenant-data"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_tenants_receive_distinct_paths_for_each_resource(self):
        tenant_a = TenantStatePartition(self.root, "tenant-a")
        tenant_b = TenantStatePartition(self.root, "tenant-b")
        path_a = tenant_a.path_for("quotas", "quota.json")
        path_b = tenant_b.path_for("quotas", "quota.json")
        self.assertNotEqual(path_a, path_b)
        self.assertEqual(path_a.parts[-3:], ("tenant-a", "quotas", "quota.json"))
        self.assertEqual(path_b.parts[-3:], ("tenant-b", "quotas", "quota.json"))

    def test_only_allowlisted_resources_and_safe_json_basenames_are_accepted(self):
        partition = TenantStatePartition(self.root, "tenant-a")
        for resource, filename in (
            ("unknown", "data.json"),
            ("quotas", "../other.json"),
            ("jobs", "/tmp/job.json"),
            ("credentials", "token.txt"),
            ("assets", "nested/file.json"),
            ("usage", "api_key.json"),
        ):
            with self.subTest(resource=resource, filename=filename), self.assertRaises(ValueError):
                partition.path_for(resource, filename)

    def test_invalid_tenant_ids_are_rejected(self):
        for tenant_id in ("", "../escape", "tenant/a", " tenant", "tenant "):
            with self.subTest(tenant_id=tenant_id), self.assertRaises(ValueError):
                TenantStatePartition(self.root, tenant_id)

    def test_initialize_creates_separate_resource_directories(self):
        partition = TenantStatePartition(self.root, "tenant-a")
        result = partition.initialize()
        self.assertEqual(result["status"], "TENANT_STORAGE_INITIALIZED")
        self.assertTrue(result["initialized"])
        for resource in partition.RESOURCES:
            self.assertTrue((partition.tenant_root / resource).is_dir())

    def test_symlink_tenant_root_fails_closed(self):
        self.root.mkdir(parents=True)
        outside = Path(self.temp_dir.name) / "outside"
        outside.mkdir()
        link = self.root / "tenant-a"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is not available in this environment")
        partition = TenantStatePartition(self.root, "tenant-a")
        self.assertEqual(partition.initialize()["status"], "TENANT_STORAGE_ROOT_SYMLINK")
        with self.assertRaises(ValueError):
            partition.path_for("jobs", "queue.json")


if __name__ == "__main__":
    unittest.main()
