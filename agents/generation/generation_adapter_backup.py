import json
import os
from datetime import datetime
from pathlib import Path


class GenerationAdapter:

    def __init__(self):
        self.name = "AI Generation Adapter"
        self.version = "1.0"

        self.providers = {
            "image": "NOT_CONNECTED",
            "video": "NOT_CONNECTED",
            "voice": "NOT_CONNECTED"
        }

    def status(self):
        return {
            "adapter": self.name,
            "version": self.version,
            "providers": self.providers,
            "status": "READY"
        }

    def prepare_image_job(self, scene):
        return {
            "type": "image",
            "scene": scene.get("scene"),
            "title": scene.get("title"),
            "prompt": scene.get("image_prompt", ""),
            "aspect_ratio": "16:9",
            "resolution": "4K",
            "status": "QUEUED"
        }

    def generate_image(self, prompt, output_path):
        import os
        from huggingface_hub import InferenceClient

        token = os.getenv("HF_TOKEN")

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
            "provider": "Hugging Face",
            "model": "stabilityai/stable-diffusion-xl-base-1.0",
            "output": output_path
        }

    def prepare_video_job(self, scene):
        return {
            "type": "video",
            "scene": scene.get("scene"),
            "title": scene.get("title"),
            "prompt": scene.get("video_prompt", ""),
            "aspect_ratio": "16:9",
            "resolution": "4K",
            "frame_rate": "24fps",
            "status": "QUEUED"
        }

    def prepare_voice_job(self, voice):
        return {
            "type": "voice",
            "scene": voice.get("scene"),
            "title": voice.get("title"),
            "narration": voice.get("narration", ""),
            "voice_style": voice.get("voice_style", ""),
            "delivery": voice.get("delivery", ""),
            "status": "QUEUED"
        }


if __name__ == "__main__":

    adapter = GenerationAdapter()
    result = adapter.status()

    os.makedirs("projects/generation", exist_ok=True)

    output = {
        "project": "World Most Popular Documentary Filming",
        "engine": adapter.name,
        "version": adapter.version,
        "status": "READY",
        "providers": result["providers"],
        "generation_modes": [
            "IMAGE",
            "VIDEO",
            "VOICE"
        ],
        "created_at": datetime.now().isoformat()
    }

    output_file = "projects/generation/generation_adapter_status.json"

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)

    print("===================================")
    print("     AI GENERATION ADAPTER")
    print("===================================")
    print()
    print("Adapter:", adapter.name)
    print("Version:", adapter.version)
    print("Image provider:", adapter.providers["image"])
    print("Video provider:", adapter.providers["video"])
    print("Voice provider:", adapter.providers["voice"])
    print()
    print("Generation modes: IMAGE / VIDEO / VOICE")
    print("Status: READY")
    print()
    print("Adapter status created successfully.")
    print("Saved to:", output_file)
