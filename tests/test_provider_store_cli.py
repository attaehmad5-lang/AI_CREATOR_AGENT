import subprocess
import sys
import unittest
from pathlib import Path


class ProviderStoreCliTests(unittest.TestCase):
    def test_cli_reports_runtime_readiness_without_claiming_unverified_ready(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-m", "projects.provider_store.provider_store_cli"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Provider catalog entries:", result.stdout)
        self.assertIn("Advanced readiness:", result.stdout)
        self.assertIn("Catalog consistency:", result.stdout)
        self.assertNotIn("Universal Flow:", result.stdout)
        if "Advanced readiness: READY" not in result.stdout:
            self.assertIn("STATUS: DEGRADED", result.stdout)
            self.assertNotIn("STATUS: READY", result.stdout)


if __name__ == "__main__":
    unittest.main()
