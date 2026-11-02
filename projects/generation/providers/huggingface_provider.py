import os
from pathlib import Path
from huggingface_hub import InferenceClient
from .base_provider import BaseProvider
from .atomic_output import atomic_save_image

class HuggingFaceProvider(BaseProvider):
    name = "Hugging Face"

    def generate_image(self, prompt, output_path):
        token = os.getenv("HF_TOKEN") or Path("hf_token.txt").read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError("HF_TOKEN is not set")

        client = InferenceClient(
            api_key=token,
            provider="auto"
        )

        image = client.text_to_image(
            prompt,
            model="stabilityai/stable-diffusion-xl-base-1.0"
        )

        destination = atomic_save_image(image, output_path)

        return {
            "status": "COMPLETED",
            "provider": self.name,
            "model": "stabilityai/stable-diffusion-xl-base-1.0",
            "output": str(destination)
        }
