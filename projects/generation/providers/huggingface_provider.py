import os
from pathlib import Path
from huggingface_hub import InferenceClient
from .base_provider import BaseProvider

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

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path)

        return {
            "status": "COMPLETED",
            "provider": self.name,
            "model": "stabilityai/stable-diffusion-xl-base-1.0",
            "output": output_path
        }
