"""Fail-closed production release readiness evaluator for Task #25."""
from __future__ import annotations

from typing import Any


class ProviderReleaseReadiness:
    """Evaluate explicit release evidence; never infer readiness from a green unit suite."""

    REQUIRED_EVIDENCE = (
        "all_features_verified",
        "cross_platform_tests_passed",
        "all_supported_modalities_e2e_passed",
        "live_credentials_verified",
        "secret_scan_passed",
        "remote_backup_verified",
        "rollback_restore_verified",
        "security_review_passed",
        "operations_runbook_approved",
    )

    def evaluate(self, evidence: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(evidence, dict):
            return {
                "status": "INVALID_RELEASE_EVIDENCE",
                "release_ready": False,
                "decision": "BLOCK",
                "missing_or_failed": list(self.REQUIRED_EVIDENCE),
            }
        failures = []
        for key in self.REQUIRED_EVIDENCE:
            if evidence.get(key) is not True:
                failures.append(key)
        open_count = evidence.get("open_features")
        partial_count = evidence.get("partial_features")
        verified_count = evidence.get("verified_features")
        total_count = evidence.get("total_features")
        counts_valid = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (open_count, partial_count, verified_count, total_count)
        )
        if not counts_valid:
            failures.append("valid_feature_counts")
        else:
            if verified_count + partial_count + open_count != total_count:
                failures.append("consistent_feature_counts")
            if open_count != 0:
                failures.append("zero_open_features")
            if partial_count != 0:
                failures.append("zero_partial_features")
            if total_count == 0 or verified_count != total_count:
                failures.append("all_features_verified_count")
        if failures:
            return {
                "status": "RELEASE_BLOCKED",
                "release_ready": False,
                "decision": "BLOCK",
                "missing_or_failed": sorted(set(failures)),
                "feature_counts": {
                    "verified": verified_count if counts_valid else None,
                    "partial": partial_count if counts_valid else None,
                    "open": open_count if counts_valid else None,
                    "total": total_count if counts_valid else None,
                },
            }
        return {
            "status": "RELEASE_READY",
            "release_ready": True,
            "decision": "PASS",
            "missing_or_failed": [],
            "feature_counts": {
                "verified": verified_count,
                "partial": partial_count,
                "open": open_count,
                "total": total_count,
            },
        }
