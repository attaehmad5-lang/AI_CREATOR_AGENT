import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.credit_manager import ProviderCreditManager
from projects.provider_store.model_pricing_manager import (
    POLLINATIONS_MODELS_URL,
    ProviderModelPricingManager,
)
from projects.provider_store.routing_manager import ProviderRoutingManager


class ProviderModelPricingManagerTests(unittest.TestCase):
    def setUp(self):
        self.now = [datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)]
        self.payload = {
            "data": [
                {
                    "id": "community/example/flux-fast",
                    "aliases": ["flux-fast"],
                    "category": "image",
                    "paid_only": False,
                    "pricing": {
                        "currency": "pollen",
                        "completionImageTokens": 0.25,
                    },
                },
                {
                    "id": "community/example/text-model",
                    "aliases": ["text-model"],
                    "category": "text",
                    "paid_only": True,
                    "pricing": {
                        "currency": "pollen",
                        "promptTextTokens": 0.000001,
                        "completionTextTokens": 0.000002,
                    },
                },
                {
                    "id": "community/example/unpriced",
                    "aliases": ["unpriced"],
                    "category": "image",
                    "pricing": {"currency": "pollen"},
                },
            ]
        }
        self.manager = ProviderModelPricingManager(
            max_age_seconds=60,
            clock=lambda: self.now[0],
            fetcher=lambda url, timeout: self.payload,
        )

    def test_refresh_uses_public_catalog_and_model_aliases(self):
        refreshed = self.manager.refresh_pollinations_catalog()
        self.assertEqual(refreshed["status"], "MODEL_PRICING_UPDATED")
        self.assertEqual(refreshed["source"], POLLINATIONS_MODELS_URL)
        self.assertEqual(refreshed["model_count"], 2)

        model = self.manager.get_model_record(
            "pollinations", "flux-fast", modality="image"
        )
        self.assertEqual(model["status"], "MODEL_FOUND")
        self.assertEqual(model["model"]["id"], "community/example/flux-fast")

    def test_image_request_estimate_uses_live_per_image_price(self):
        self.manager.refresh_pollinations_catalog()
        estimate = self.manager.estimate_request(
            provider_id="pollinations",
            model_id="flux-fast",
            modality="image",
            usage={"completion_images": 2},
        )
        self.assertEqual(estimate["status"], "MODEL_PRICE_ESTIMATED")
        self.assertEqual(estimate["estimated_cost"], 0.5)
        self.assertEqual(estimate["unit"], "POLLEN")
        self.assertEqual(estimate["components"], {"completion_images": 0.5})
        self.assertEqual(estimate["source"], POLLINATIONS_MODELS_URL)

    def test_text_estimate_requires_every_billable_usage_component(self):
        self.manager.refresh_pollinations_catalog()
        incomplete = self.manager.estimate_request(
            provider_id="pollinations",
            model_id="text-model",
            modality="text",
            usage={"prompt_text_tokens": 1000},
        )
        self.assertEqual(incomplete["status"], "PRICING_USAGE_INCOMPLETE")
        self.assertEqual(incomplete["missing_component"], "completion_text_tokens")

        complete = self.manager.estimate_request(
            provider_id="pollinations",
            model_id="text-model",
            modality="text",
            usage={"prompt_text_tokens": 1000, "completion_text_tokens": 500},
        )
        self.assertEqual(complete["status"], "MODEL_PRICE_ESTIMATED")
        self.assertAlmostEqual(complete["estimated_cost"], 0.002)

    def test_stale_catalog_never_authorizes_a_price_estimate(self):
        self.manager.refresh_pollinations_catalog()
        self.now[0] += timedelta(seconds=61)
        result = self.manager.estimate_request(
            provider_id="pollinations",
            model_id="flux-fast",
            modality="image",
            usage={"completion_images": 1},
        )
        self.assertEqual(result["status"], "MODEL_PRICING_STALE")
        self.assertEqual(
            self.manager.get_model_record("pollinations", "flux-fast")["status"],
            "MODEL_CATALOG_STALE",
        )

    def test_missing_prices_are_not_interpreted_as_zero_cost(self):
        self.manager.refresh_pollinations_catalog()
        result = self.manager.estimate_request(
            provider_id="pollinations",
            model_id="unpriced",
            modality="image",
            usage={"completion_images": 1},
        )
        self.assertEqual(result["status"], "MODEL_PRICE_NOT_FOUND")

    def test_unknown_price_fields_are_excluded_instead_of_underestimated(self):
        self.payload["data"].append({
            "id": "future-pricing-shape",
            "category": "image",
            "pricing": {"currency": "pollen", "futureImageCharge": 0.1},
        })
        refreshed = self.manager.refresh_pollinations_catalog()
        self.assertEqual(refreshed["status"], "MODEL_PRICING_UPDATED")
        self.assertEqual(
            self.manager.estimate_request(
                provider_id="pollinations",
                model_id="future-pricing-shape",
                modality="image",
                usage={"completion_images": 1},
            )["status"],
            "MODEL_PRICE_NOT_FOUND",
        )

    def test_invalid_usage_and_currency_mismatch_fail_closed(self):
        self.manager.refresh_pollinations_catalog()
        invalid = self.manager.estimate_request(
            provider_id="pollinations",
            model_id="flux-fast",
            modality="image",
            usage={"completion_images": True},
        )
        self.assertEqual(invalid["status"], "PRICING_USAGE_INCOMPLETE")
        unsupported = self.manager.estimate_request(
            provider_id="huggingface",
            model_id="flux-fast",
            modality="image",
            usage={"completion_images": 1},
        )
        self.assertEqual(unsupported["status"], "MODEL_PRICE_NOT_SUPPORTED")

    def test_refresh_error_does_not_leak_upstream_exception_text(self):
        def raising_fetcher(url, *, timeout):
            raise RuntimeError("private response body token=secret")

        manager = ProviderModelPricingManager(fetcher=raising_fetcher)
        result = manager.refresh_pollinations_catalog()
        self.assertEqual(result["status"], "MODEL_PRICING_REFRESH_FAILED")
        self.assertNotIn("secret", repr(result))
        self.assertNotIn("private response body", repr(result))


class ProviderModelPricingRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog_path = self.root / "providers.json"
        self.state_path = self.root / "connections.json"
        self.catalog_path.write_text(json.dumps({
            "providers": [{
                "id": "pollinations",
                "name": "Pollinations",
                "category": "image",
                "auth_type": "API_KEY",
                "connection_type": "API",
                "enabled": True,
                "subscription_required": False,
                "zero_cost_allowed": False,
                "capabilities": ["image"],
            }]
        }), encoding="utf-8")
        self.state_path.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(self.catalog_path, self.state_path)
        verified = self.connections.verify_connectivity(
            "pollinations", lambda provider, **kwargs: {"ok": True}
        )
        self.assertTrue(verified["verified"])

        self.pricing = ProviderModelPricingManager(
            fetcher=lambda url, timeout: {
                "data": [{
                    "id": "community/example/flux-fast",
                    "aliases": ["flux-fast"],
                    "category": "image",
                    "pricing": {"currency": "pollen", "completionImageTokens": 0.25},
                }]
            }
        )
        self.pricing.refresh_pollinations_catalog()
        self.credit_state = self.root / "credits.json"
        self.credits = ProviderCreditManager(self.credit_state)
        self.credits.refresh(
            "pollinations",
            lambda _: {
                "verified": True,
                "free_credits_remaining": 2.0,
                "unit": "POLLEN",
                "source": "official-account-api",
            },
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_router_uses_fresh_model_price_and_live_model_metadata(self):
        router = ProviderRoutingManager(
            self.connections,
            credit_manager=self.credits,
            model_pricing_manager=self.pricing,
        )
        result = router.route(
            category="image",
            project_id="film",
            model="flux-fast",
            strategy="LOWEST_COST",
            estimated_cost_unit="POLLEN",
            pricing_usage={"completion_images": 1},
            request_id="priced-route-1",
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["provider"], "pollinations")
        self.assertEqual(result["estimated_cost"], 0.25)
        self.assertEqual(result["estimated_cost_unit"], "POLLEN")
        self.assertEqual(result["model_price_evidence"]["source"], POLLINATIONS_MODELS_URL)
        self.assertTrue(result["credit_reserved"])

    def test_lowest_cost_routing_rejects_unpriced_competitor(self):
        self.catalog_path.write_text(json.dumps({
            "providers": [
                {
                    "id": "pollinations", "name": "Pollinations", "category": "image",
                    "auth_type": "API_KEY", "connection_type": "API", "enabled": True,
                    "subscription_required": False, "zero_cost_allowed": False,
                    "capabilities": ["image"],
                },
                {
                    "id": "competitor", "name": "Competitor", "category": "image",
                    "auth_type": "NONE", "connection_type": "API", "enabled": True,
                    "zero_cost_allowed": False, "capabilities": ["image"],
                    "supported_models": [{"id": "flux-fast"}],
                },
            ]
        }), encoding="utf-8")
        self.connections = ConnectionManager(self.catalog_path, self.state_path)
        for provider_id in ("pollinations", "competitor"):
            self.assertTrue(self.connections.verify_connectivity(
                provider_id, lambda provider, **kwargs: {"ok": True}
            )["verified"])
        router = ProviderRoutingManager(
            self.connections,
            credit_manager=self.credits,
            model_pricing_manager=self.pricing,
        )
        result = router.route(
            category="image",
            project_id="film",
            model="flux-fast",
            strategy="LOWEST_COST",
            estimated_cost_unit="POLLEN",
            pricing_usage={"completion_images": 1},
            request_id="priced-route-2",
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "MODEL_PRICE_COMPARISON_INCOMPLETE")
        self.assertIn(
            {"provider": "competitor", "status": "MODEL_PRICE_NOT_SUPPORTED"},
            result["decisions"],
        )

    def test_live_price_unit_mismatch_is_never_converted_implicitly(self):
        router = ProviderRoutingManager(
            self.connections,
            credit_manager=self.credits,
            model_pricing_manager=self.pricing,
        )
        result = router.route(
            category="image",
            project_id="film",
            model="flux-fast",
            strategy="LOWEST_COST",
            estimated_cost_unit="USD",
            pricing_usage={"completion_images": 1},
            request_id="priced-route-3",
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "MODEL_PRICE_COMPARISON_INCOMPLETE")
        self.assertIn(
            {"provider": "pollinations", "status": "MODEL_PRICE_UNIT_MISMATCH"},
            result["decisions"],
        )


if __name__ == "__main__":
    unittest.main()
