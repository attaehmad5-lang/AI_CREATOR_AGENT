import unittest

from projects.provider_store.provider_release_readiness import ProviderReleaseReadiness


class ProviderReleaseReadinessTests(unittest.TestCase):
    def setUp(self):
        self.gate = ProviderReleaseReadiness()
        self.evidence = {
            "all_features_verified": True,
            "cross_platform_tests_passed": True,
            "all_supported_modalities_e2e_passed": True,
            "live_credentials_verified": True,
            "secret_scan_passed": True,
            "remote_backup_verified": True,
            "rollback_restore_verified": True,
            "security_review_passed": True,
            "operations_runbook_approved": True,
            "verified_features": 138,
            "partial_features": 0,
            "open_features": 0,
            "total_features": 138,
        }

    def test_release_pass_requires_all_explicit_evidence_and_zero_incomplete_features(self):
        result = self.gate.evaluate(self.evidence)
        self.assertEqual(result["status"], "RELEASE_READY")
        self.assertTrue(result["release_ready"])
        self.assertEqual(result["decision"], "PASS")

    def test_partial_or_open_features_block_release_even_with_green_tests(self):
        for key, value in (("partial_features", 1), ("open_features", 1)):
            evidence = dict(self.evidence)
            evidence[key] = value
            evidence["verified_features"] = 137
            evidence["partial_features"] = 1 if key == "partial_features" else 0
            evidence["open_features"] = 1 if key == "open_features" else 0
            result = self.gate.evaluate(evidence)
            self.assertEqual(result["status"], "RELEASE_BLOCKED")
            self.assertFalse(result["release_ready"])
            self.assertIn("all_features_verified", result["missing_or_failed"])

    def test_missing_live_modality_or_backup_evidence_blocks_release(self):
        for key in ("all_supported_modalities_e2e_passed", "live_credentials_verified",
                    "remote_backup_verified", "rollback_restore_verified"):
            evidence = dict(self.evidence)
            evidence[key] = False
            result = self.gate.evaluate(evidence)
            self.assertEqual(result["decision"], "BLOCK")
            self.assertIn(key, result["missing_or_failed"])

    def test_inconsistent_and_boolean_counts_fail_closed(self):
        for evidence in (
            {**self.evidence, "verified_features": True},
            {**self.evidence, "total_features": 137},
            {**self.evidence, "verified_features": -1},
        ):
            result = self.gate.evaluate(evidence)
            self.assertEqual(result["status"], "RELEASE_BLOCKED")
            self.assertFalse(result["release_ready"])

    def test_invalid_evidence_shape_blocks_release(self):
        for evidence in (None, [], "ready"):
            result = self.gate.evaluate(evidence)
            self.assertEqual(result["status"], "INVALID_RELEASE_EVIDENCE")
            self.assertEqual(result["decision"], "BLOCK")


if __name__ == "__main__":
    unittest.main()
