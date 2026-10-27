"""Tests that production provider dependencies can be injected at the orchestrator boundary."""
import unittest
from unittest.mock import patch

from agents.orchestrator.master_orchestrator import MasterOrchestrator


class OrchestratorProviderStoreInjectionTests(unittest.TestCase):
    def test_orchestrator_forwards_provider_store_dependencies(self):
        service = object()
        executor = lambda **kwargs: {"started": False}
        with patch("agents.orchestrator.master_orchestrator.CreativeBrain"), \
             patch("agents.orchestrator.master_orchestrator.MasterJobEngine") as job_engine, \
             patch("agents.orchestrator.master_orchestrator.ResearchEngine"), \
             patch("agents.orchestrator.master_orchestrator.IdeaEngine"), \
             patch("agents.orchestrator.master_orchestrator.ScriptEngine"):
            orchestrator = MasterOrchestrator(
                provider_store_service=service,
                provider_store_adapter=executor,
                job_storage_path="jobs-test.json",
            )

        job_engine.assert_called_once_with(
            provider_store_service=service,
            provider_store_adapter=executor,
            storage_path="jobs-test.json",
        )
        self.assertEqual(orchestrator.version, "1.1")


if __name__ == "__main__":
    unittest.main()
