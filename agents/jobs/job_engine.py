import uuid
import json
import os
from datetime import datetime
from agents.execution.execution_gateway import ExecutionGateway

class MasterJobEngine:

    def __init__(self, *, provider_store_service=None, provider_store_adapter=None, storage_path=None):
        self.version = "1.1"
        self.status = "READY"
        self.storage_path = storage_path or "agents/jobs/jobs.json"
        # Production hosts can explicitly inject the canonical provider service
        # and trusted executor. No provider executor is loaded from catalog data.
        self.gateway = ExecutionGateway(
            provider_store_service=provider_store_service,
            provider_store_adapter=provider_store_adapter,
        )
        self.stage_handlers = {"CREATED": self.handle_created, "PLANNING": self.handle_planning, "SCRIPT": self.handle_script, "SCENES": self.handle_scenes, "GENERATION": self.handle_generation, "AUDIO": self.handle_audio, "SUBTITLES": self.handle_subtitles, "EDITING": self.handle_editing, "QUALITY_CONTROL": self.handle_quality_control, "COMPLETED": self.handle_completed}

        self.stages = ["CREATED", "PLANNING", "SCRIPT", "SCENES", "GENERATION", "AUDIO", "SUBTITLES", "EDITING", "QUALITY_CONTROL", "COMPLETED"]

    def create_job(self, request):
        return {
            "job_id": str(uuid.uuid4()),
            "status": "CREATED",
            "current_stage": "CREATED",
            "created_at": datetime.utcnow().isoformat(),
            "request": request,
            "stages": []
        }

    def save_job(self, job):
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        jobs = []
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    jobs = json.load(f)
            except Exception:
                jobs = []
        jobs = [x for x in jobs if x.get("job_id") != job.get("job_id")]
        jobs.append(job)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(jobs, f, indent=2, ensure_ascii=False)
        return job

    def load_jobs(self):
        if not os.path.exists(self.storage_path):
            return []
        with open(self.storage_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def update_stage(self, job, stage):
        if stage not in self.stages:
            raise ValueError("Unknown stage: " + stage)
        job["status"] = stage
        job["current_stage"] = stage
        return job

    def _set_stage_result(self, job, stage, status="READY", **details):
        job["stage_result"] = {"stage": stage, "status": status, **details}
        return job

    def handle_created(self, job, stage=None):
        return self._set_stage_result(job, "CREATED", request_received=True)

    def handle_planning(self, job, stage=None):
        request = job.get("request", {})
        topic = request.get("topic", "")
        language = request.get("language", "auto")
        duration = request.get("duration", "auto")
        content_type = request.get("type", "cinematic_video")
        idea_result = job.get("idea_result", {})
        brain_plan = job.get("brain_analysis", {}).get("production_plan", {})
        plan = {"topic": topic, "language": language, "duration": duration, "content_type": content_type, "research_required": brain_plan.get("research", True), "script_required": brain_plan.get("script", True), "scene_breakdown_required": brain_plan.get("storyboard", True), "visual_generation_required": brain_plan.get("visual_generation", True), "audio_required": brain_plan.get("audio", True), "subtitles_required": brain_plan.get("subtitles", True), "editing_required": brain_plan.get("editing", True), "quality_control_required": brain_plan.get("quality_control", True), "ideas": idea_result.get("ideas", []), "concepts": idea_result.get("concepts", []), "hooks": idea_result.get("hooks", [])}
        job["production_plan"] = plan
        return self._set_stage_result(job, "PLANNING", plan_ready=True, production_plan=plan)

    def handle_script(self, job, stage=None):
        request = job.get("request", {})
        plan = job.get("production_plan", {})
        script_result = job.get("script_result", {})
        job["script_plan"] = {"topic": plan.get("topic", request.get("topic", "")), "language": plan.get("language", request.get("language", "auto")), "duration": plan.get("duration", request.get("duration", "auto")), "content_type": plan.get("content_type", request.get("type", "general")), "ideas": script_result.get("ideas", plan.get("ideas", [])), "concepts": script_result.get("concepts", plan.get("concepts", [])), "hooks": script_result.get("hooks", plan.get("hooks", [])), "script": script_result.get("script", []), "scenes": script_result.get("scenes", []), "dialogue": script_result.get("dialogue", []), "script_status": "READY" if script_result.get("script") else "PENDING_AI_WRITER"}
        return self._set_stage_result(job, "SCRIPT", script_ready=bool(job["script_plan"].get("script")), script_plan=job["script_plan"], next="SCENES_ENGINE" if job["script_plan"].get("script") else "SCRIPT_ENGINE")

    def handle_scenes(self, job, stage=None):
        plan = job.get("production_plan", {})
        script_plan = job.get("script_plan", {})
        job["scene_plan"] = {"topic": script_plan.get("topic", plan.get("topic", "")), "content_type": script_plan.get("content_type", plan.get("content_type", "general")), "language": script_plan.get("language", plan.get("language", "auto")), "duration": script_plan.get("duration", plan.get("duration", "auto")), "script": script_plan.get("script", []), "scenes": script_plan.get("scenes", []), "dialogue": script_plan.get("dialogue", []), "storyboard_status": "READY" if script_plan.get("scenes") else "PENDING_SCENE_ENGINE"}
        return self._set_stage_result(job, "SCENES", storyboard_ready=bool(job["scene_plan"].get("scenes")), scene_plan=job["scene_plan"], next="GENERATION_ENGINE" if job["scene_plan"].get("scenes") else "SCENE_ENGINE")

    def handle_generation(self, job, stage=None):
        scene_plan = job.get("scene_plan", {})
        job["generation_plan"] = {"topic": scene_plan.get("topic", ""), "content_type": scene_plan.get("content_type", "general"), "language": scene_plan.get("language", "auto"), "duration": scene_plan.get("duration", "auto"), "script": scene_plan.get("script", []), "scenes": scene_plan.get("scenes", []), "dialogue": scene_plan.get("dialogue", []), "visual_generation": True, "image_generation": True, "video_generation": True, "provider_routing": "UNIFIED_PROVIDER_ROUTER", "generation_status": "READY" if scene_plan.get("scenes") else "PENDING_GENERATION_GATEWAY"}
        generated_outputs = []
        for scene in job["generation_plan"].get("scenes", []):
            scene_id = scene.get("scene", len(generated_outputs) + 1)
            prompt = scene.get("description", "") or job["generation_plan"].get("topic", "")
            output_path = f"output/job_{job.get("job_id", "unknown")}_scene_{scene_id}.png"
            result = self.execute_capability("text_to_image", {"prompt": prompt, "output_path": output_path})
            generated_outputs.append({"scene": scene_id, "result": result})
        job["generation_plan"]["generated_outputs"] = generated_outputs
        job["generation_plan"]["generation_status"] = "COMPLETED" if generated_outputs and all(x.get("result", {}).get("status") == "COMPLETED" for x in generated_outputs) else job["generation_plan"].get("generation_status", "READY")
        return self._set_stage_result(job, "GENERATION", generation_ready=bool(job["generation_plan"].get("scenes")), generation_plan=job["generation_plan"], next="GENERATION_GATEWAY" if job["generation_plan"].get("scenes") else "GENERATION_GATEWAY")

    def handle_audio(self, job, stage=None):
        generation_plan = job.get("generation_plan", {})
        job["audio_plan"] = {"topic": generation_plan.get("topic", ""), "content_type": generation_plan.get("content_type", "general"), "script": generation_plan.get("script", []), "scenes": generation_plan.get("scenes", []), "dialogue": generation_plan.get("dialogue", []), "generated_outputs": generation_plan.get("generated_outputs", []), "voice_required": True, "music_required": True, "sfx_required": True, "voice_provider_routing": "UNIFIED_PROVIDER_ROUTER", "music_provider_routing": "UNIFIED_PROVIDER_ROUTER", "audio_status": "READY" if generation_plan.get("scenes") else "PENDING_AUDIO_ENGINE"}
        return self._set_stage_result(job, "AUDIO", voice_ready=bool(job["audio_plan"].get("dialogue")), music_ready=bool(job["audio_plan"].get("scenes")), sfx_ready=bool(job["audio_plan"].get("scenes")), audio_plan=job["audio_plan"], next="AUDIO_ENGINE")

    def handle_subtitles(self, job, stage=None):
        audio_plan = job.get("audio_plan", {})
        job["subtitle_plan"] = {"topic": audio_plan.get("topic", ""), "content_type": audio_plan.get("content_type", "general"), "language": audio_plan.get("language", job.get("request", {}).get("language", "auto")), "script": audio_plan.get("script", []), "scenes": audio_plan.get("scenes", []), "dialogue": audio_plan.get("dialogue", []), "captions_required": True, "subtitle_generation": True, "subtitle_status": "READY" if audio_plan.get("dialogue") or audio_plan.get("script") else "PENDING_SUBTITLE_ENGINE"}
        return self._set_stage_result(job, "SUBTITLES", subtitles_ready=bool(job["subtitle_plan"].get("dialogue") or job["subtitle_plan"].get("script")), subtitle_plan=job["subtitle_plan"], next="EDITING_ENGINE")

    def handle_editing(self, job, stage=None):
        subtitle_plan = job.get("subtitle_plan", {}); generation_plan = job.get("generation_plan", {}); audio_plan = job.get("audio_plan", {})
        job["editing_plan"] = {"topic": subtitle_plan.get("topic", ""), "content_type": subtitle_plan.get("content_type", "general"), "language": subtitle_plan.get("language", "auto"), "script": subtitle_plan.get("script", []), "scenes": generation_plan.get("scenes", subtitle_plan.get("scenes", [])), "generated_outputs": generation_plan.get("generated_outputs", []), "dialogue": audio_plan.get("dialogue", subtitle_plan.get("dialogue", [])), "visuals_input": bool(generation_plan.get("generated_outputs") or generation_plan.get("scenes")), "audio_input": bool(audio_plan.get("dialogue") or audio_plan.get("scenes")), "subtitles_input": bool(subtitle_plan.get("dialogue") or subtitle_plan.get("script")), "timeline_editing": True, "final_render": True, "editing_status": "READY" if (generation_plan.get("generated_outputs") or generation_plan.get("scenes")) else "PENDING_VIDEO_EDITOR"}
        return self._set_stage_result(job, "EDITING", edit_ready=bool(job["editing_plan"].get("visuals_input")), final_render_ready=bool(job["editing_plan"].get("visuals_input") and job["editing_plan"].get("audio_input") and job["editing_plan"].get("subtitles_input")), editing_plan=job["editing_plan"], next="QUALITY_CONTROL")

    def handle_quality_control(self, job, stage=None):
        editing_plan = job.get("editing_plan", {})
        production_plan = job.get("brain_analysis", {}).get("production_plan", {})
        visual_required = production_plan.get("visual_generation", True)
        audio_required = production_plan.get("audio", True)
        subtitles_required = production_plan.get("subtitles", True)
        editing_required = production_plan.get("editing", True)
        generation_outputs = job.get("generation_plan", {}).get("generated_outputs", [])
        generation_ok = (not visual_required) or (bool(generation_outputs) and all(x.get("result", {}).get("status") == "COMPLETED" for x in generation_outputs))
        visual_ok = (not visual_required) or bool(editing_plan.get("visuals_input"))
        audio_ok = (not audio_required) or bool(editing_plan.get("audio_input"))
        subtitles_ok = (not subtitles_required) or bool(editing_plan.get("subtitles_input"))
        render_ok = (not editing_required) or bool(editing_plan.get("final_render"))
        passed = generation_ok and visual_ok and audio_ok and subtitles_ok and render_ok
        job["quality_plan"] = {"topic": editing_plan.get("topic", ""), "content_type": editing_plan.get("content_type", "general"), "language": editing_plan.get("language", "auto"), "visual_quality_check": visual_ok, "audio_quality_check": audio_ok, "subtitle_quality_check": subtitles_ok, "sync_check": (not (audio_required and subtitles_required)) or bool(editing_plan.get("audio_input") and editing_plan.get("subtitles_input")), "render_check": render_ok, "generation_quality_check": generation_ok, "final_quality_status": "READY" if passed else "FAILED"}
        return self._set_stage_result(job, "QUALITY_CONTROL", quality_passed=passed, quality_plan=job["quality_plan"], next="COMPLETED" if passed else "QC_ENGINE")

    def handle_completed(self, job, stage=None):
        editing_plan = job.get("editing_plan", {})
        quality_plan = job.get("quality_plan", {})
        final_output = job.get("final_output") or {"topic": job.get("request", {}).get("topic", editing_plan.get("topic", "")), "content_type": job.get("request", {}).get("type", editing_plan.get("content_type", "general")), "visuals": editing_plan.get("generated_outputs", []), "scenes": editing_plan.get("scenes", []), "dialogue": editing_plan.get("dialogue", []), "subtitles": job.get("subtitle_plan", {}).get("script", []), "quality_status": quality_plan.get("final_quality_status", "PENDING"), "status": "READY" if quality_plan.get("final_quality_status") == "READY" else "BLOCKED"}
        job["final_output"] = final_output
        final_status = "READY" if final_output.get("status") == "READY" else "BLOCKED"
        return self._set_stage_result(job, "COMPLETED", status=final_status, final_output=final_output)

    def handle_stage(self, job, stage=None):
        stage = stage or job.get("status", "CREATED")
        job["stage_result"] = {"stage": stage, "status": "READY"}
        return job

    def run_job(self, job):
        self.save_job(job)
        while True:
            current_stage = job.get("current_stage", job.get("status", "CREATED"))
            handler = self.stage_handlers.get(current_stage)
            if handler:
                job = handler(job, current_stage)
            self.save_job(job)
            if job.get("current_stage") == "COMPLETED":
                break
            job = self.advance_stage(job)
            self.save_job(job)
        return job

    def advance_stage(self, job):
        current = job.get("current_stage", job.get("status", "CREATED"))
        if current == "COMPLETED":
            return job
        if current not in self.stages:
            current = "CREATED"
        plan = job.get("brain_analysis", {}).get("production_plan", {})
        stage_flags = {"SCRIPT": "script", "SCENES": "storyboard", "GENERATION": "visual_generation", "AUDIO": "audio", "SUBTITLES": "subtitles", "EDITING": "editing", "QUALITY_CONTROL": "quality_control"}
        i = self.stages.index(current)
        while i < len(self.stages) - 1:
            i += 1
            next_stage = self.stages[i]
            flag = stage_flags.get(next_stage)
            if flag is None or plan.get(flag, True):
                return self.update_stage(job, next_stage)
        return job

    def execute_capability(self, capability, payload=None):
        return self.gateway.execute(capability, payload)

    def status_info(self):
        return {
            "engine": "Master Job Engine",
            "version": self.version,
            "status": self.status
        }

if __name__ == "__main__":
    engine = MasterJobEngine()

    print("===================================")
    print("       MASTER JOB ENGINE")
    print("===================================")
    print("STATUS:", engine.status)
    print("READY")
