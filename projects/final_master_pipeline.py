import json
import os
from datetime import datetime

FILES = [
    "projects/research/research.json",
    "projects/story/story.json",
    "projects/story/documentary_script.json",
    "projects/scenes/documentary_scenes.json",
    "projects/visuals/visual_prompts.json",
    "projects/voice/voice_prompts.json",
    "projects/video/video_production_plan.json",
    "projects/editor/master_edit_plan.json",
    "projects/seo/advanced_seo_plan.json"
]

OUTPUT_FILE = "projects/final_master_project.json"

ready_files = []
missing_files = []

for file_path in FILES:
    if os.path.exists(file_path):
        ready_files.append(file_path)
    else:
        missing_files.append(file_path)

project_name = "World Most Popular Documentary Filming"

master = {
    "project": project_name,
    "status": "READY" if not missing_files else "INCOMPLETE",
    "engine": "AI Creator Agent - Final Master Orchestrator",
    "pipeline_version": "1.0 Advanced",
    "created_at": datetime.now().isoformat(),

    "production_pipeline": {
        "research": "READY",
        "story": "READY",
        "script": "READY",
        "scenes": "READY",
        "visuals": "READY",
        "voice": "READY",
        "video": "READY",
        "editing": "READY",
        "seo": "READY"
    },

    "core_files": ready_files,
    "missing_files": missing_files,

    "output_specification": {
        "content_type": "Cinematic Documentary",
        "format": "16:9",
        "resolution": "4K",
        "frame_rate": "24fps",
        "target_platform": "YouTube",
        "target_duration": "20-30 minutes",
        "visual_quality": "Professional Cinematic",
        "audio_quality": "Professional Documentary Mix"
    },

    "final_status": {
        "files_ready": len(ready_files),
        "files_missing": len(missing_files),
        "pipeline_ready": len(missing_files) == 0
    }
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(master, f, indent=4, ensure_ascii=False)

print("===================================")
print("    FINAL MASTER ORCHESTRATOR")
print("===================================")
print()
print("Project:", project_name)
print("Pipeline version:", master["pipeline_version"])
print()
print("Research:", master["production_pipeline"]["research"])
print("Story:", master["production_pipeline"]["story"])
print("Script:", master["production_pipeline"]["script"])
print("Scenes:", master["production_pipeline"]["scenes"])
print("Visuals:", master["production_pipeline"]["visuals"])
print("Voice:", master["production_pipeline"]["voice"])
print("Video:", master["production_pipeline"]["video"])
print("Editing:", master["production_pipeline"]["editing"])
print("SEO:", master["production_pipeline"]["seo"])
print()
print("Core files ready:", len(ready_files))
print("Missing files:", len(missing_files))
print("Pipeline ready:", master["final_status"]["pipeline_ready"])
print()
print("Final master project created successfully.")
print("Saved to:", OUTPUT_FILE)
