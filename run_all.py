import subprocess
import sys

STAGES = [
    ("Documentary Pipeline", "projects/run_pipeline.py"),
    ("Script Generator", "projects/story/script_generator.py"),
    ("Scene Builder", "projects/scenes/cinematic_scene_builder.py"),
    ("Visual Prompt Engine", "projects/visuals/visual_prompt_generator.py"),
    ("Voice Engine", "projects/voice/advanced_voice_engine.py"),
    ("Video Production Engine", "projects/video/advanced_video_engine.py"),
    ("Master Editing Engine", "projects/editor/advanced_editor_engine.py"),
    ("SEO Publishing Engine", "projects/seo/advanced_seo_engine.py"),
    ("Master Orchestrator", "projects/final_master_pipeline.py"),
    ("Production Audit", "projects/final_production_audit.py")
]

print("===================================")
print("       AI CREATOR AGENT")
print("      MASTER RUNNER v1.0")
print("===================================")
print()

for name, script in STAGES:
    print()
    print(">>>", name)
    print("-----------------------------------")

    result = subprocess.run(
        [sys.executable, script],
        capture_output=False
    )

    if result.returncode != 0:
        print()
        print("PIPELINE STOPPED")
        print("Failed stage:", name)
        print("File:", script)
        sys.exit(result.returncode)

print()
print("===================================")
print("     AI CREATOR PIPELINE DONE")
print("===================================")
print()
print("All production stages completed.")
print("System status: READY")
