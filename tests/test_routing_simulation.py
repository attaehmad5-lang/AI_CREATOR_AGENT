import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.routing_manager import ProviderRoutingManager


class RoutingSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = self.root / "providers.json"
        self.state = self.root / "connections.json"
        self.catalog.write_text(
            json.dumps(
                {
                    "providers": [
                        {
                            "id": "primary",
                            "name": "Primary",
                            "category": "image",
                            "auth_type": "NONE",
                            "connection_type": "API",
                            "enabled": True,
                            "zero_cost_allowed": True,
                            "priority": 1,
                            "capabilities": ["image"],
                        },
                        {
                            "id": "fallback",
                            "name": "Fallback",
                            "category": "image",
                            "auth_type": "NONE",
                            "connection_type": "API",
                            "enabled": True,
                            "zero_cost_allowed": True,
                            "priority": 2,
                            "capabilities": ["image"],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.state.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        self.connections = ConnectionManager(self.catalog, self.state)
        for provider_id in ("primary", "fallback"):
            result = self.connections.verify_connectivity(
                provider_id, lambda provider, **kwargs: {"ok": True}
            )
            self.assertTrue(result["verified"])

        class QuotaStub:
            def __init__(self):
                self.calls = []

            def reserve(self, *, provider_id, request_id):
                self.calls.append((provider_id, request_id))
                return {"status": "RESERVED"}

        self.quota = QuotaStub()
        self.router = ProviderRoutingManager(
            self.connections,
            quota_manager=self.quota,
            default_strategy="ROUND_ROBIN",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_previews_route_without_reserving_or_advancing_round_robin(self):
        options = {
            "category": "image",
            "project_id": "demo",
            "request_id": "preview-1",
            "dry_run": True,
        }
        first = self.router.route(**options)
        second = self.router.route(**{**options, "request_id": "preview-2"})

        self.assertTrue(first["allowed"])
        self.assertEqual(first["status"], "ROUTE_SIMULATED")
        self.assertEqual(first["provider"], "primary")
        self.assertEqual(first["would_reserve"], ["quota"])
        self.assertTrue(first["dry_run"])
        self.assertEqual(second["provider"], "primary")
        self.assertEqual(self.quota.calls, [])
        self.assertEqual(self.router._round_robin_counters, {})

        committed = self.router.route(
            category="image", project_id="demo", request_id="actual-1"
        )
        self.assertEqual(committed["status"], "ROUTED")
        self.assertEqual(committed["provider"], "primary")
        self.assertEqual(self.quota.calls, [("primary", "actual-1")])
        self.assertEqual(self.router._round_robin_counters["image"], 1)

        next_route = self.router.route(
            category="image", project_id="demo", request_id="actual-2"
        )
        self.assertEqual(next_route["provider"], "fallback")

    def test_invalid_dry_run_flag_fails_closed(self):
        result = self.router.route(
            category="image", project_id="demo", dry_run="yes"
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "INVALID_DRY_RUN_FLAG")
        self.assertEqual(self.quota.calls, [])


if __name__ == "__main__":
    unittest.main()
