"""Cost-gated, deterministic provider A/B experiment tests."""
import unittest

from projects.provider_store.provider_ab_experiment_manager import ProviderABExperimentManager


class ProviderABExperimentManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = ProviderABExperimentManager(
            approval_verifier=lambda **claim: {
                "allowed": True,
                "approved_max_cost_per_request": claim["requested_max_cost_per_request"],
            }
        )

    @staticmethod
    def variants():
        return [
            {
                "variant_id": "control",
                "provider_id": "provider-a",
                "model_id": "model-a",
                "weight": 70,
                "usage_mode": "FREE_VERIFIED",
                "approval_ref": "free-evidence-2026",
                "max_cost_per_request": 0.0,
            },
            {
                "variant_id": "candidate",
                "provider_id": "provider-b",
                "model_id": "model-b",
                "weight": 30,
                "usage_mode": "PAID_APPROVED",
                "approval_ref": "spend-approval-2026",
                "max_cost_per_request": 0.25,
            },
        ]

    def configure(self, **overrides):
        values = {
            "experiment_id": "image-quality-test",
            "modality": "image",
            "variants": self.variants(),
            "traffic_percent": 100,
        }
        values.update(overrides)
        return self.manager.configure(**values)

    def test_configure_verifies_free_and_paid_variants_and_does_not_store_refs(self):
        result = self.configure()
        self.assertEqual(result["status"], "EXPERIMENT_CONFIGURED")
        self.assertTrue(result["configured"])
        variants = result["experiment"]["variants"]
        self.assertEqual(variants[0]["approved_max_cost_per_request"], 0.0)
        self.assertEqual(variants[1]["approved_max_cost_per_request"], 0.25)
        self.assertNotIn("approval_ref", variants[0])
        self.assertNotIn("approval_ref", variants[1])
        stored = self.manager.get_experiment("image-quality-test")["experiment"]
        self.assertNotIn("approval_ref", stored["variants"][0])

    def test_paid_variant_is_blocked_without_approval_verifier(self):
        manager = ProviderABExperimentManager()
        result = manager.configure(
            experiment_id="no-approval", modality="image",
            variants=self.variants(),
        )
        self.assertEqual(result["status"], "USAGE_APPROVAL_VERIFIER_UNAVAILABLE")
        self.assertFalse(result["configured"])

    def test_denied_malformed_and_underfunded_approvals_fail_closed(self):
        denied = ProviderABExperimentManager(approval_verifier=lambda **_: {"allowed": False})
        underfunded = ProviderABExperimentManager(approval_verifier=lambda **_: {
            "allowed": True, "approved_max_cost_per_request": 0.01,
        })
        malformed = ProviderABExperimentManager(approval_verifier=lambda **_: {
            "allowed": True, "approved_max_cost_per_request": float("nan"),
        })
        for manager, expected in (
            (denied, "USAGE_NOT_APPROVED"),
            (underfunded, "APPROVED_COST_LIMIT_INSUFFICIENT"),
            (malformed, "APPROVED_COST_LIMIT_INSUFFICIENT"),
        ):
            result = manager.configure(
                experiment_id="blocked", modality="image",
                variants=self.variants(),
            )
            self.assertEqual(result["status"], expected)
            self.assertFalse(result["configured"])

    def test_approval_verifier_exception_is_redacted_and_blocks_configuration(self):
        def fail(**_):
            raise RuntimeError("sensitive approval backend detail")
        manager = ProviderABExperimentManager(approval_verifier=fail)
        result = manager.configure(
            experiment_id="approval-error", modality="image",
            variants=self.variants(),
        )
        self.assertEqual(result["status"], "USAGE_APPROVAL_CHECK_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("sensitive", str(result))

    def test_free_variant_cannot_claim_zero_cost_without_zero_cost_approval(self):
        manager = ProviderABExperimentManager(approval_verifier=lambda **_: {
            "allowed": True, "approved_max_cost_per_request": 0.01,
        })
        result = manager.configure(
            experiment_id="false-free", modality="image",
            variants=self.variants(),
        )
        self.assertEqual(result["status"], "APPROVED_COST_LIMIT_INSUFFICIENT")

    def test_invalid_weights_duplicate_variants_and_nonzero_free_cost_are_rejected(self):
        variants = self.variants()
        variants[0]["weight"] = 60
        self.assertEqual(
            self.configure(variants=variants)["status"],
            "VARIANT_WEIGHTS_MUST_TOTAL_100",
        )
        variants = self.variants()
        variants[1]["variant_id"] = "control"
        self.assertEqual(
            self.configure(variants=variants)["status"],
            "DUPLICATE_VARIANT",
        )
        variants = self.variants()
        variants[0]["max_cost_per_request"] = 0.01
        self.assertEqual(
            self.configure(variants=variants)["status"],
            "FREE_VARIANT_HAS_NONZERO_COST",
        )

    def test_malformed_variant_mode_and_boolean_weight_are_rejected_without_exception(self):
        variants = self.variants()
        variants[0]["usage_mode"] = []
        self.assertEqual(
            self.configure(variants=variants)["status"],
            "INVALID_USAGE_MODE",
        )
        variants = self.variants()
        variants[0]["weight"] = True
        self.assertEqual(
            self.configure(variants=variants)["status"],
            "INVALID_VARIANT_WEIGHT",
        )

    def test_assignment_is_deterministic_and_requires_downstream_cost_gate(self):
        self.configure()
        first = self.manager.assign(
            experiment_id="image-quality-test", request_id="request-123",
        )
        second = self.manager.assign(
            experiment_id="image-quality-test", request_id="request-123",
        )
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "VARIANT_ASSIGNED")
        assignment = first["assignment"]
        self.assertTrue(assignment["provider_call_permitted"])
        self.assertTrue(assignment["downstream_cost_gate_required"])
        self.assertFalse(assignment["execution_performed"])
        self.assertIn(assignment["usage_mode"], {"FREE_VERIFIED", "PAID_APPROVED"})
        if assignment["usage_mode"] == "PAID_APPROVED":
            self.assertEqual(assignment["spend_limit_per_request"], 0.25)
        else:
            self.assertEqual(assignment["spend_limit_per_request"], 0.0)

    def test_traffic_percentage_keeps_nonexperiment_requests_out_of_provider_calls(self):
        self.configure(traffic_percent=0)
        result = self.manager.assign(
            experiment_id="image-quality-test", request_id="request-123",
        )
        self.assertEqual(result["status"], "CONTROL_ONLY")
        self.assertFalse(result["assignment"]["provider_call_permitted"])
        self.assertFalse(result["assignment"]["execution_performed"])

    def test_traffic_split_and_variant_weights_are_deterministic_and_bounded(self):
        self.configure(traffic_percent=25)
        control_only = 0
        selected = 0
        for index in range(4000):
            result = self.manager.assign(
                experiment_id="image-quality-test", request_id=f"req-{index}",
            )
            if result["status"] == "CONTROL_ONLY":
                control_only += 1
            else:
                selected += 1
        self.assertGreater(control_only, 2800)
        self.assertLess(control_only, 3200)
        self.assertGreater(selected, 800)
        self.assertLess(selected, 1200)

    def test_disabled_unknown_and_invalid_request_assignments_fail_closed(self):
        self.configure()
        self.assertTrue(self.manager.set_enabled("image-quality-test", False)["updated"])
        self.assertEqual(
            self.manager.assign(
                experiment_id="image-quality-test", request_id="request-1",
            )["status"],
            "EXPERIMENT_DISABLED",
        )
        self.assertEqual(
            self.manager.assign(experiment_id="missing", request_id="request-1")["status"],
            "EXPERIMENT_NOT_FOUND",
        )
        self.assertEqual(
            self.manager.assign(
                experiment_id="image-quality-test", request_id="bad request",
            )["status"],
            "INVALID_REQUEST_ID",
        )

    def test_experiment_configuration_is_immutable_and_duplicate_ids_are_blocked(self):
        result = self.configure()
        result["experiment"]["variants"][0]["provider_id"] = "attacker"
        self.assertEqual(
            self.manager.get_experiment("image-quality-test")["experiment"]["variants"][0]["provider_id"],
            "provider-a",
        )
        self.assertEqual(self.configure()["status"], "EXPERIMENT_ALREADY_EXISTS")

    def test_zero_traffic_and_experiment_capacity_are_bounded(self):
        manager = ProviderABExperimentManager(
            approval_verifier=self.manager.approval_verifier,
            max_experiments=1,
        )
        first = manager.configure(
            experiment_id="first", modality="image",
            variants=self.variants(), traffic_percent=0,
        )
        self.assertTrue(first["configured"])
        second = manager.configure(
            experiment_id="second", modality="image",
            variants=self.variants(), traffic_percent=0,
        )
        self.assertEqual(second["status"], "EXPERIMENT_CAPACITY_REACHED")


if __name__ == "__main__":
    unittest.main()
