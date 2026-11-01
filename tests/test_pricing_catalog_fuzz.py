"""Deterministic malformed-input fuzz regressions for the pricing catalog."""
import random
import unittest

from projects.provider_store.pricing_catalog import PricingCatalog


class PricingCatalogFuzzTests(unittest.TestCase):
    def test_seeded_malformed_records_never_escape_schema_validation(self):
        rng = random.Random(680114)
        atoms = [
            None, True, False, 0, -1, 1.5, float("nan"), "",
            "provider-a", "../bad", [], {}, [1, 2], {"nested": "value"},
        ]
        keys = [
            "price_id", "provider_id", "model_id", "modality", "pricing_unit",
            "unit_price", "currency", "source", "verified_at",
            "effective_from", "effective_to", "notes", "api_key",
        ]
        for _ in range(1000):
            record = {
                rng.choice(keys): rng.choice(atoms)
                for _ in range(rng.randint(0, len(keys)))
            }
            result = PricingCatalog._validate_record(record)
            self.assertIsInstance(result, bool)

    def test_seeded_estimate_arguments_always_return_structured_results(self):
        rng = random.Random(680115)
        atoms = [None, True, 0, -1, 2.5, "bad", [], {}, "provider-a", "model-a", "USD"]
        catalog = PricingCatalog("missing-pricing-catalog-for-fuzz.json")
        for _ in range(1000):
            result = catalog.estimate(
                provider_id=rng.choice(atoms),
                model_id=rng.choice(atoms),
                modality=rng.choice(atoms),
                units=rng.choice(atoms),
                currency=rng.choice(atoms),
                as_of=rng.choice(atoms),
            )
            self.assertIsInstance(result, dict)
            self.assertIn("status", result)
            self.assertIn("estimated", result)


if __name__ == "__main__":
    unittest.main()
