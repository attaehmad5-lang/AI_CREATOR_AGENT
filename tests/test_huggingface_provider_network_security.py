"""Hugging Face image adapter must install the pinned transport before inference."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from projects.generation.providers.huggingface_provider import HuggingFaceProvider


class HuggingFaceProviderNetworkSecurityTests(unittest.TestCase):
    def test_pinned_transport_is_installed_before_client_creation(self):
        order = []
        image = object()
        client = type("Client", (), {
            "text_to_image": lambda self, prompt, model: image,
        })()
        with patch.dict(os.environ, {"HF_TOKEN": "test-token"}, clear=False), patch(
            "projects.generation.providers.huggingface_provider.install_huggingface_pinned_transport",
            side_effect=lambda: order.append("pinned-transport"),
        ) as install, patch(
            "projects.generation.providers.huggingface_provider.InferenceClient",
            side_effect=lambda **kwargs: (order.append("inference-client") or client),
        ) as client_factory, patch(
            "projects.generation.providers.huggingface_provider.atomic_save_image",
            return_value=Path("test-output.png"),
        ) as save_image:
            result = HuggingFaceProvider().generate_image("cinematic test prompt", "test-output.png")

        self.assertEqual(order, ["pinned-transport", "inference-client"])
        install.assert_called_once_with()
        client_factory.assert_called_once_with(
            api_key="test-token", provider="auto", timeout=180,
        )
        client.text_to_image.assert_called_once_with(
            "cinematic test prompt",
            model="stabilityai/stable-diffusion-xl-base-1.0",
        )
        save_image.assert_called_once_with(image, "test-output.png")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["output"], "test-output.png")

    def test_transport_installation_failure_blocks_inference(self):
        with patch.dict(os.environ, {"HF_TOKEN": "test-token"}, clear=False), patch(
            "projects.generation.providers.huggingface_provider.install_huggingface_pinned_transport",
            side_effect=RuntimeError("pinned transport unavailable"),
        ), patch(
            "projects.generation.providers.huggingface_provider.InferenceClient"
        ) as client_factory:
            with self.assertRaisesRegex(RuntimeError, "pinned transport unavailable"):
                HuggingFaceProvider().generate_image("prompt", "out.png")
        client_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
