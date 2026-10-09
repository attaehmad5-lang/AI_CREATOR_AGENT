import json
import os
from datetime import datetime


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
