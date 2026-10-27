"""Regression checks for Provider Store workflow path filters.

These tests intentionally use only the standard library so they run in the
same minimal CI environment as the project.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "provider-store-integration.yml"


def _event_paths(text: str, event: str, next_event: str) -> list[str]:
    match = re.search(
        rf"^  {re.escape(event)}:\n(.*?)(?=^  {re.escape(next_event)}:)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"Workflow event block not found: {event}")
    paths_match = re.search(
        r"^    paths:\n((?:^      - \"[^\"]+\"\n)+)",
        match.group(1),
        flags=re.MULTILINE,
    )
    if not paths_match:
        raise AssertionError(f"Path filters not found for event: {event}")
    return re.findall(r'^      - "([^"]+)"$', paths_match.group(1), re.MULTILINE)


class ProviderStoreWorkflowFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow_text = WORKFLOW.read_text(encoding="utf-8")
        cls.pull_request_paths = _event_paths(cls.workflow_text, "pull_request", "push")
        cls.push_paths = _event_paths(cls.workflow_text, "push", "workflow_dispatch")

    def test_pull_request_paths_have_no_duplicates(self):
        self.assertEqual(len(self.pull_request_paths), len(set(self.pull_request_paths)))

    def test_push_paths_have_no_duplicates(self):
        self.assertEqual(len(self.push_paths), len(set(self.push_paths)))

    def test_push_filter_covers_every_pull_request_path(self):
        self.assertEqual(
            set(self.pull_request_paths) - set(self.push_paths),
            set(),
            "Every path tested on pull requests must also trigger push verification.",
        )

    def test_security_integration_paths_are_watched(self):
        required = {
            "projects/provider_store/artifact_scanner.py",
            "projects/provider_store/oauth2_refresh_manager.py",
            "tests/test_artifact_scanner.py",
            "tests/test_oauth2_refresh_manager.py",
            "tests/test_provider_store_workflow_filters.py",
            "agents/jobs/job_engine.py",
            "agents/orchestrator/master_orchestrator.py",
            "tests/test_job_engine_provider_store_injection.py",
            "tests/test_orchestrator_provider_store_injection.py",
        }
        self.assertTrue(required.issubset(set(self.pull_request_paths)))
        self.assertTrue(required.issubset(set(self.push_paths)))


if __name__ == "__main__":
    unittest.main()
