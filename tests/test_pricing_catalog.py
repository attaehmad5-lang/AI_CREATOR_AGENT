import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.pricing_catalog import PricingCatalog


class PricingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "pricing.json"
        self.records = [
            {
                "price_id": "image-standard-2026",
                "provider_id": "provider-a",
                "model_id": "image-v2",
                "modality": "image",
                "pricing_unit": "image",
                "unit_price": 0.04,
                "currency": "USD",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "source": "official-provider-pricing-page",
                "verified_at": "2026-10-01T12:00:00+00:00",
            },
            {
                "price_id": "text-token-2026",
                "provider_id": "provider-a",
                "model_id": "text-v3",
                "modality": "text",
                "pricing_unit": "1k_tokens",
                "unit_price": 0.002,
                "currency": "USD",
                "effective_from": "2026-01-01",
                "source": "official-provider-pricing-page",
                "verified_at": "2026-10-01T12:00:00+00:00",
            },
        ]
        self.write_catalog(self.records)
        self.catalog = PricingCatalog(self.path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_catalog(self, records):
        self.path.write_text(
            json.dumps({"version": "1.0", "prices": records}),
            encoding="utf-8",
        )

    def test_exact_image_cost_estimate_is_source_attributed(self):
        result = self.catalog.estimate(
            provider_id="provider-a", model_id="image-v2",
            modality="image", units=10, currency="USD",
            as_of="2026-06-01",
        )
        self.assertEqual(result["status"], "COST_ESTIMATED")
        self.assertEqual(result["estimated_cost"], 0.4)
        self.assertEqual(result["source"], "official-provider-pricing-page")
        self.assertFalse(result["billing_authoritative"])

    def test_per_thousand_token_pricing_uses_correct_unit_scale(self):
        result = self.catalog.estimate(
            provider_id="provider-a", model_id="text-v3",
            modality="text", units=1500, currency="USD",
            as_of="2026-06-01",
        )
        self.assertEqual(result["estimated_cost"], 0.003)
        self.assertEqual(result["pricing_unit"], "1k_tokens")

    def test_effective_dates_and_currency_are_enforced(self):
        expired = self.catalog.estimate(
            provider_id="provider-a", model_id="image-v2",
            modality="image", units=1, currency="USD",
            as_of="2027-01-01",
        )
        wrong_currency = self.catalog.estimate(
            provider_id="provider-a", model_id="image-v2",
            modality="image", units=1, currency="EUR",
            as_of="2026-06-01",
        )
        self.assertEqual(expired["status"], "PRICING_NOT_FOUND")
        self.assertEqual(wrong_currency["status"], "PRICING_NOT_FOUND")

    def test_overlapping_prices_are_reported_as_ambiguous(self):
        duplicate = dict(self.records[0], price_id="image-standard-duplicate")
        self.write_catalog(self.records + [duplicate])
        catalog = PricingCatalog(self.path)
        result = catalog.estimate(
            provider_id="provider-a", model_id="image-v2",
            modality="image", units=1, currency="USD",
            as_of="2026-06-01",
        )
        self.assertEqual(result["status"], "PRICING_AMBIGUOUS")

    def test_invalid_inputs_and_missing_prices_fail_closed(self):
        self.assertEqual(self.catalog.estimate(
            provider_id="provider-a", model_id="image-v2",
            modality=[], units=1, currency="USD",
        )["status"], "INVALID_ESTIMATE_INPUT")
        self.assertEqual(self.catalog.estimate(
            provider_id="provider-a", model_id="image-v2",
            modality="image", units=0, currency="USD",
        )["status"], "INVALID_ESTIMATE_INPUT")
        self.assertEqual(self.catalog.estimate(
            provider_id="provider-a", model_id="missing",
            modality="image", units=1, currency="USD",
        )["status"], "PRICING_NOT_FOUND")
        self.assertEqual(self.catalog.estimate(
            provider_id="provider-a", model_id="image-v2",
            modality="image", units=1, currency="USD",
            as_of="not-a-date",
        )["status"], "INVALID_ESTIMATE_DATE")

    def test_invalid_catalog_record_shapes_do_not_raise(self):
        for patch in (
            {"modality": []},
            {"pricing_unit": {}},
            {"currency": []},
            {"unit_price": True},
            {"provider_id": "../bad"},
            {"api_key": "must-not-be-stored"},
        ):
            with self.subTest(patch=patch):
                self.write_catalog([dict(self.records[0], **patch)])
                result = PricingCatalog(self.path).list_prices()
                self.assertEqual(result["status"], "PRICING_NOT_FOUND")

    def test_upsert_atomically_adds_and_replaces_valid_price_records(self):
        record = dict(self.records[0], price_id="managed-price")
        saved = self.catalog.upsert_price(record)
        self.assertEqual(saved["status"], "PRICE_SAVED")
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["count"], 3)

        replacement = dict(record, unit_price=0.05)
        updated = self.catalog.upsert_price(replacement)
        self.assertEqual(updated["status"], "PRICE_SAVED")
        self.assertEqual(updated["count"], 3)
        reloaded = PricingCatalog(self.path)
        result = reloaded.estimate(
            provider_id="provider-a", model_id="image-v2",
            modality="image", units=2, currency="USD",
            as_of="2026-06-01",
        )
        self.assertEqual(result["estimated_cost"], 0.1)

    def test_upsert_rejects_invalid_record_and_preserves_existing_catalog(self):
        original = self.path.read_bytes()
        result = self.catalog.upsert_price(dict(self.records[0], unit_price=float("nan")))
        self.assertEqual(result["status"], "INVALID_PRICE_RECORD")
        self.assertFalse(result["saved"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_upsert_atomic_write_failure_preserves_original_and_cleans_temp(self):
        from unittest.mock import patch
        original = self.path.read_bytes()
        with patch(
            "projects.provider_store.pricing_catalog.atomic_replace_with_retry",
            side_effect=OSError("simulated replace failure"),
        ):
            result = self.catalog.upsert_price(
                dict(self.records[0], price_id="write-failure-price")
            )
        self.assertEqual(result["status"], "PRICING_CATALOG_WRITE_FAILED")
        self.assertFalse(result["saved"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".pricing.json.*.tmp")), [])

    def test_upsert_rejects_catalog_symlink_without_touching_target(self):
        target = self.root / "protected-pricing.json"
        target.write_bytes(self.path.read_bytes())
        link = self.root / "linked-pricing.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        result = PricingCatalog(link).upsert_price(dict(self.records[0], price_id="nope"))
        self.assertEqual(result["status"], "PRICING_CATALOG_SYMLINK_REJECTED")
        self.assertEqual(target.read_bytes(), self.path.read_bytes())

    def test_duplicate_json_keys_are_rejected(self):
        self.path.write_text(
            '{"version":"1.0","version":"1.0","prices":[]}',
            encoding="utf-8",
        )
        result = PricingCatalog(self.path).list_prices()
        self.assertEqual(result["status"], "PRICING_NOT_FOUND")

    def test_catalog_symlinks_are_rejected(self):
        target = Path(self.temp_dir.name) / "target.json"
        target.write_text('{"version":"1.0","prices":[]}', encoding="utf-8")
        link = Path(self.temp_dir.name) / "link.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        result = PricingCatalog(link).list_prices()
        self.assertEqual(result["status"], "PRICING_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
