import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class GenerationAdapter:

    def __init__(self, *, provider_store_service=None, provider_store_adapter=None):
        self.name = "AI Generation Adapter"
        self.version = "1.1"
        # Opt-in canonical execution boundary. If configured, failures never
        # silently fall back to the legacy generation service.
        self.provider_store_service = provider_store_service
        self.provider_store_adapter = provider_store_adapter

        self.providers = {
            "image": "NOT_CONNECTED",
            "video": "NOT_CONNECTED",
            "voice": "NOT_CONNECTED"
        }

        if self.provider_store_service is None:
            try:
                from projects.generation.providers.provider_service import ProviderService
                service = ProviderService()
                image_config = service.registry.get("image", {})
                if image_config.get("status") == "CONNECTED":
                    self.providers["image"] = image_config.get("primary", "NOT_CONNECTED")
            except Exception:
                pass

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

    def generate_image(
        self, prompt, output_path, *, request_id=None, project_id="default",
        estimated_cost=0.0, route_options=None, adapter_options=None,
    ):
        if self.provider_store_service is not None:
            return self._generate_image_via_provider_store(
                prompt, output_path, request_id=request_id, project_id=project_id,
                estimated_cost=estimated_cost, route_options=route_options,
                adapter_options=adapter_options,
            )

        # Preserve legacy behavior until a host explicitly injects the canonical
        # ProviderService and a provider executor. This keeps old deployments
        # working while preventing a configured canonical path from bypassing
        # its safety/accounting boundary.
        from projects.generation.providers.provider_service import ProviderService

        service = ProviderService()
        result = service.generate_image(prompt, output_path)
        if isinstance(result, dict) and result.get("status") == "COMPLETED":
            self.providers["image"] = result.get("provider", "UNKNOWN")
        return result

    def _generate_image_via_provider_store(
        self, prompt, output_path, *, request_id=None, project_id="default",
        estimated_cost=0.0, route_options=None, adapter_options=None,
    ):
        service = self.provider_store_service
        executor = self.provider_store_adapter
        if not callable(getattr(service, "execute_routed_request", None)):
            return {"status": "PROVIDER_STORE_SERVICE_INVALID", "executed": False}
        if not callable(executor):
            return {"status": "PROVIDER_EXECUTOR_UNAVAILABLE", "executed": False}
        if not isinstance(prompt, str) or not prompt.strip():
            return {"status": "INVALID_PROMPT", "executed": False}
        if not isinstance(project_id, str) or not project_id.strip():
            return {"status": "INVALID_PROJECT_ID", "executed": False}
        if not isinstance(route_options, (dict, type(None))):
            return {"status": "INVALID_ROUTE_OPTIONS", "executed": False}
        if not isinstance(adapter_options, (dict, type(None))):
            return {"status": "INVALID_ADAPTER_OPTIONS", "executed": False}
        if request_id is None:
            request_id = "generation-" + uuid4().hex
        if not isinstance(request_id, str) or not request_id.strip():
            return {"status": "INVALID_REQUEST_ID", "executed": False}

        options = dict(adapter_options or {})
        options.update({"prompt": prompt, "output_path": output_path})

        def invoke_provider(**routed):
            # ProviderService injects the selected provider and request ID.
            # The host executor owns provider-specific request formatting.
            merged_options = dict(options)
            merged_options.update(routed)
            return executor(**merged_options)

        try:
            result = service.execute_routed_request(
                category="image",
                project_id=project_id,
                request_id=request_id,
                estimated_cost=estimated_cost,
                adapter=invoke_provider,
                modality="image",
                output_path=output_path,
                adapter_options=options,
                **dict(route_options or {}),
            )
        except Exception as exc:
            # The service call itself may have crossed the network boundary.
            return {
                "status": "EXECUTION_OUTCOME_UNKNOWN",
                "executed": False,
                "reservation_state": "UNKNOWN",
                "manual_reconciliation_required": True,
                "request_id": request_id,
                "error_type": type(exc).__name__,
            }
        if isinstance(result, dict) and result.get("status") in {
            "COMPLETED", "EXECUTION_SUCCEEDED", "PROVIDER_EXECUTION_SUCCEEDED"
        }:
            self.providers["image"] = result.get("provider", "UNKNOWN")
        return result if isinstance(result, dict) else {
            "status": "EXECUTION_OUTCOME_UNKNOWN",
            "executed": False,
            "reservation_state": "UNKNOWN",
            "manual_reconciliation_required": True,
            "request_id": request_id,
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
