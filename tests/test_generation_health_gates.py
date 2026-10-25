"""Regression tests for portable provider capability and master health gates."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProviderCapabilityGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.module = load_module(
            "provider_capability_gate_under_test",
            "projects/generation/provider_capability_gate.py",
        )
        self.module.CONNECTION = self.directory / "connection.json"
        self.module.REGISTRY = self.directory / "registry.json"
        self.module.OUTPUT = self.directory / "gate.json"

    def write_inputs(self, connected=True, capabilities=None):
        capabilities = capabilities or {
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": ["text_to_video", "image_to_video"]},
            "voice": {"capabilities": ["text_to_speech"]},
        }
        connections = [
            {"media_type": media, "connected": connected}
            for media in ("IMAGE", "VIDEO", "VOICE")
        ]
        self.module.CONNECTION.write_text(
            json.dumps({"connections": connections}), encoding="utf-8"
        )
        self.module.REGISTRY.write_text(json.dumps(capabilities), encoding="utf-8")

    def test_all_declared_capabilities_connected_reports_ready(self):
        self.write_inputs()
        result = self.module.main()
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["passed"], 3)
        self.assertEqual(result["blocked"], 0)
        self.assertTrue(self.module.OUTPUT.is_file())
        self.assertFalse(result["real_provider_call"])

    def test_disconnected_providers_are_blocked(self):
        self.write_inputs(connected=False)
        result = self.module.main()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["passed"], 0)
        self.assertEqual(result["blocked"], 3)


    def test_missing_input_fails_closed_without_exception(self):
        self.module.REGISTRY.write_text(json.dumps({
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": ["text_to_video", "image_to_video"]},
            "voice": {"capabilities": ["text_to_speech"]},
        }), encoding="utf-8")
        result = self.module.main()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["input_valid"])
        self.assertEqual(result["passed"], 0)
        self.assertTrue(self.module.OUTPUT.is_file())

    def test_malformed_json_fails_closed_without_exception(self):
        self.module.CONNECTION.write_text("{bad json", encoding="utf-8")
        self.module.REGISTRY.write_text(json.dumps({
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": ["text_to_video", "image_to_video"]},
            "voice": {"capabilities": ["text_to_speech"]},
        }), encoding="utf-8")
        result = self.module.main()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["input_valid"])
        self.assertEqual(result["blocked"], 3)

    def test_wrong_json_shape_fails_closed(self):
        self.module.CONNECTION.write_text(json.dumps({"connections": "not-a-list"}), encoding="utf-8")
        self.module.REGISTRY.write_text(json.dumps({
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": ["text_to_video", "image_to_video"]},
            "voice": {"capabilities": ["text_to_speech"]},
        }), encoding="utf-8")
        result = self.module.main()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["input_valid"])
        self.assertEqual(result["passed"], 0)


    def test_missing_connections_field_fails_closed(self):
        self.module.CONNECTION.write_text(json.dumps({}), encoding="utf-8")
        self.module.REGISTRY.write_text(json.dumps({
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": ["text_to_video", "image_to_video"]},
            "voice": {"capabilities": ["text_to_speech"]},
        }), encoding="utf-8")
        result = self.module.main()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["input_valid"])
        self.assertEqual(result["input_error_type"], "ValueError")

    def test_missing_provider_entry_fails_closed(self):
        self.module.CONNECTION.write_text(json.dumps({"connections": []}), encoding="utf-8")
        self.module.REGISTRY.write_text(json.dumps({
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": ["text_to_video", "image_to_video"]},
        }), encoding="utf-8")
        result = self.module.main()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["input_valid"])
        self.assertEqual(result["input_error_type"], "ValueError")


    def test_duplicate_connection_records_fail_closed(self):
        self.module.CONNECTION.write_text(json.dumps({"connections": [
            {"media_type": "IMAGE", "connected": True},
            {"media_type": "IMAGE", "connected": True},
        ]}), encoding="utf-8")
        self.module.REGISTRY.write_text(json.dumps({
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": ["text_to_video", "image_to_video"]},
            "voice": {"capabilities": ["text_to_speech"]},
        }), encoding="utf-8")
        result = self.module.main()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["input_valid"])

    def test_non_boolean_connection_state_fails_closed(self):
        self.module.CONNECTION.write_text(json.dumps({"connections": [
            {"media_type": "IMAGE", "connected": "true"},
        ]}), encoding="utf-8")
        self.module.REGISTRY.write_text(json.dumps({
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": ["text_to_video", "image_to_video"]},
            "voice": {"capabilities": ["text_to_speech"]},
        }), encoding="utf-8")
        result = self.module.main()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["input_valid"])

    def test_missing_capability_degrades_when_another_modality_passes(self):
        capabilities = {
            "image": {"capabilities": ["text_to_image"]},
            "video": {"capabilities": []},
            "voice": {"capabilities": ["text_to_speech"]},
        }
        self.write_inputs(capabilities=capabilities)
        result = self.module.main()
        self.assertEqual(result["status"], "DEGRADED")
        self.assertEqual(result["passed"], 2)
        self.assertEqual(result["blocked"], 1)


class MasterSystemHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.module = load_module(
            "master_system_health_under_test",
            "projects/generation/master_system_health.py",
        )
        self.module.GEN = self.directory
        names = {
            "pipeline": "pipeline.json",
            "controller": "controller.json",
            "connection": "connection.json",
            "capability_gate": "provider_capability_gate.json",
            "orchestration": "orchestration.json",
            "state": "state.json",
            "assets": "assets.json",
        }
        self.module.FILES = {
            name: self.directory / filename for name, filename in names.items()
        }

    def write_state(self, pipeline_ok=True, capabilities_ready=True):
        values = {
            "pipeline": {"pipeline_integrity": pipeline_ok},
            "controller": {},
            "connection": {"providers_connected": 1},
            "capability_gate": {"all_capabilities_ready": capabilities_ready},
            "orchestration": {"total_jobs": 0, "waiting_for_provider": 0,
                              "waiting_for_dependency": 0},
            "state": {},
            "assets": {"total_assets": 0},
        }
        for name, path in self.module.FILES.items():
            path.write_text(json.dumps(values[name]), encoding="utf-8")

    def test_healthy_state_reports_healthy(self):
        self.write_state()
        result = self.module.main()
        self.assertEqual(result["health"], "HEALTHY")
        self.assertEqual(result["status"], "HEALTHY")
        self.assertTrue((self.directory / "master_system_health.json").is_file())

    def test_failed_pipeline_reports_blocked_not_ready(self):
        self.write_state(pipeline_ok=False)
        result = self.module.main()
        self.assertEqual(result["health"], "BLOCKED")
        self.assertEqual(result["status"], "BLOCKED")

    def test_capability_gap_reports_degraded(self):
        self.write_state(capabilities_ready=False)
        result = self.module.main()
        self.assertEqual(result["health"], "DEGRADED")
        self.assertEqual(result["status"], "DEGRADED")

    def test_missing_state_file_reports_degraded(self):
        self.write_state()
        self.module.FILES["assets"].unlink()
        result = self.module.main()
        self.assertEqual(result["health"], "DEGRADED")
        self.assertFalse(next(item for item in result["file_checks"]
                              if item["component"] == "assets")["valid"])


if __name__ == "__main__":
    unittest.main()
