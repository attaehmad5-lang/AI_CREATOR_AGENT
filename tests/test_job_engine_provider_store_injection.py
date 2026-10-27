"""Tests that the job engine can wire the canonical provider store explicitly."""
import unittest

from agents.jobs.job_engine import MasterJobEngine


class JobEngineProviderStoreInjectionTests(unittest.TestCase):
    def test_injected_service_and_executor_reach_execution_gateway(self):
        service = object()
        executor = lambda **kwargs: {"started": False}
        engine = MasterJobEngine(
            provider_store_service=service,
            provider_store_adapter=executor,
            storage_path="unused-test-jobs.json",
        )

        self.assertIs(engine.gateway.adapter.provider_store_service, service)
        self.assertIs(engine.gateway.adapter.provider_store_adapter, executor)
        self.assertEqual(engine.storage_path, "unused-test-jobs.json")

    def test_missing_production_dependencies_are_not_fabricated(self):
        engine = MasterJobEngine(storage_path="unused-test-jobs.json")

        self.assertIsNone(engine.gateway.adapter.provider_store_service)
        self.assertIsNone(engine.gateway.adapter.provider_store_adapter)


if __name__ == "__main__":
    unittest.main()
