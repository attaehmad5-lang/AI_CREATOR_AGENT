"""Output quality scoring and threshold policy tests."""
import unittest

from projects.provider_store.provider_quality_policy import ProviderOutputQualityPolicy


class ProviderOutputQualityPolicyTests(unittest.TestCase):
    def test_task_specific_threshold_is_enforced(self):
        policy = ProviderOutputQualityPolicy(
            lambda path, **kwargs: {"score": 0.65},
            thresholds={"documentary:text": 0.8, "image": 0.5},
            default_threshold=0.7,
        )
        low = policy.evaluate("asset:script", modality="text", category="documentary")
        self.assertFalse(low["allowed"])
        self.assertEqual(low["status"], "QUALITY_BELOW_THRESHOLD")
        self.assertEqual(low["threshold"], 0.8)
        accepted = policy.evaluate("asset:image", modality="image", category="image")
        self.assertTrue(accepted["allowed"])

    def test_invalid_scores_and_scorer_errors_fail_closed(self):
        for value in (float("nan"), float("inf"), -0.1, 1.1, "high"):
            with self.subTest(value=value):
                result = ProviderOutputQualityPolicy(
                    lambda path, **kwargs: value
                ).evaluate("asset:1", modality="image", category="image")
                self.assertEqual(result["status"], "INVALID_QUALITY_SCORE")
                self.assertFalse(result["allowed"])
        failed = ProviderOutputQualityPolicy(
            lambda path, **kwargs: (_ for _ in ()).throw(RuntimeError("private details"))
        ).evaluate("asset:1", modality="image", category="image")
        self.assertEqual(failed["status"], "QUALITY_SCORING_FAILED")
        self.assertNotIn("private details", repr(failed))

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            ProviderOutputQualityPolicy(None)
        with self.assertRaises(ValueError):
            ProviderOutputQualityPolicy(lambda *_: 0.5, default_threshold=1.1)
        with self.assertRaises(ValueError):
            ProviderOutputQualityPolicy(lambda *_: 0.5, thresholds={"image": float("nan")})


    def test_score_boundaries_and_boolean_scores_are_handled_strictly(self):
        accepted = ProviderOutputQualityPolicy(
            lambda path, **kwargs: {"score": 1},
            thresholds={"image:image": 1},
        ).evaluate("asset:1", modality="image", category="image")
        self.assertTrue(accepted["allowed"])
        self.assertEqual(accepted["score"], 1.0)

        rejected = ProviderOutputQualityPolicy(
            lambda path, **kwargs: 0,
            thresholds={"image:image": 0.1},
        ).evaluate("asset:2", modality="image", category="image")
        self.assertFalse(rejected["allowed"])
        self.assertEqual(rejected["score"], 0.0)

        for value in (True, False, {}, {"score": True}, {"score": None}):
            with self.subTest(value=value):
                result = ProviderOutputQualityPolicy(
                    lambda path, **kwargs: value
                ).evaluate("asset:3", modality="image", category="image")
                self.assertFalse(result["allowed"])
                self.assertEqual(result["status"], "INVALID_QUALITY_SCORE")

    def test_scorer_receives_only_requested_asset_context(self):
        calls = []
        policy = ProviderOutputQualityPolicy(
            lambda path, **kwargs: calls.append((path, kwargs)) or 0.9,
            thresholds={"documentary": 0.8},
        )
        result = policy.evaluate(
            "/private/output.mp4", modality="video", category="documentary"
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(calls, [(
            "/private/output.mp4",
            {"modality": "video", "category": "documentary"},
        )])
        self.assertNotIn("/private/output.mp4", repr(result))


if __name__ == "__main__":
    unittest.main()
