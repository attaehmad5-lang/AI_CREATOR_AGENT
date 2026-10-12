import unittest
from unittest.mock import Mock

from agents.execution.execution_gateway import ExecutionGateway


class ExecutionGatewayPolicyTests(unittest.TestCase):
    def test_image_generation_never_bypasses_policy_executor(self):
        router = Mock()
        router.route.return_value = {
            "status": "ROUTABLE", "available": [{"id": "huggingface"}],
            "route_selected": False,
        }
        gateway = ExecutionGateway(router=router)
        gateway.adapter.generate_image = Mock(return_value={"status": "COMPLETED"})
        result = gateway.execute("text_to_image", {
            "prompt": "cinematic landscape", "output_path": "/tmp/output.png",
        })
        self.assertEqual(result["status"], "POLICY_AWARE_EXECUTOR_REQUIRED")
        self.assertFalse(result["executed"])
        gateway.adapter.generate_image.assert_not_called()
        router.route.assert_called_once_with("text_to_image")

    def test_image_generation_delegates_to_policy_executor(self):
        router = Mock()
        executor = Mock()
        executor.execute.return_value = {
            "status": "COMPLETED", "executed": True, "provider": "huggingface",
        }
        gateway = ExecutionGateway(router=router, policy_executor=executor)
        payload = {
            "prompt": "cinematic landscape", "output_path": "/tmp/output.png",
            "project_id": "film", "request_id": "job-1", "estimated_cost": 0.0,
        }
        result = gateway.execute("text_to_image", payload)
        self.assertEqual(result["status"], "COMPLETED")
        executor.execute.assert_called_once_with(payload)
        router.route.assert_not_called()

    def test_policy_executor_exception_fails_closed(self):
        router = Mock()
        executor = Mock()
        executor.execute.side_effect = RuntimeError("internal failure")
        gateway = ExecutionGateway(router=router, policy_executor=executor)
        result = gateway.execute("text_to_image", {
            "prompt": "cinematic landscape", "output_path": "/tmp/output.png",
        })
        self.assertEqual(result["status"], "POLICY_EXECUTION_FAILED")
        self.assertFalse(result["executed"])
        self.assertEqual(result["error_type"], "RuntimeError")

    def test_non_generation_capability_keeps_discovery_behavior(self):
        router = Mock()
        router.route.return_value = {
            "status": "PROVIDERS_FOUND", "available": [],
            "locked": [{"id": "future-provider"}],
        }
        gateway = ExecutionGateway(router=router)
        result = gateway.execute("video_generation")
        self.assertEqual(result["status"], "PROVIDERS_FOUND")
        self.assertEqual(result["locked"], [{"id": "future-provider"}])
        self.assertFalse(result["payload_received"])


if __name__ == "__main__":
    unittest.main()
