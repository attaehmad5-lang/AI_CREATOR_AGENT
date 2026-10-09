import json
import os

FILES = {
    "research": "projects/research/research.json",
    "story": "projects/story/story.json",
    "script": "projects/story/documentary_script.json",
    "scenes": "projects/scenes/documentary_scenes.json",
    "visuals": "projects/visuals/visual_prompts.json",
    "voice": "projects/voice/voice_prompts.json",
    "video": "projects/video/video_production_plan.json",
    "editor": "projects/editor/master_edit_plan.json",
    "seo": "projects/seo/advanced_seo_plan.json",
    "master": "projects/final_master_project.json"
}

results = []
errors = []

def load_json(name, path):
    if not os.path.exists(path):
        errors.append(f"{name}: FILE MISSING")
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        results.append(f"{name}: JSON VALID")
        return data

    except Exception as e:
        errors.append(f"{name}: INVALID JSON - {e}")
        return None


data = {}

for name, path in FILES.items():
    data[name] = load_json(name, path)


if data["script"]:
    sections = data["script"].get("sections", [])
    if len(sections) == 10:
        results.append("SCRIPT: 10 SECTIONS VERIFIED")
    else:
        errors.append(f"SCRIPT: EXPECTED 10 SECTIONS, FOUND {len(sections)}")


if data["scenes"]:
    scenes = data["scenes"].get("scenes", [])
    if len(scenes) == 10:
        results.append("SCENES: 10 SCENES VERIFIED")
    else:
        errors.append(f"SCENES: EXPECTED 10, FOUND {len(scenes)}")


if data["visuals"]:
    visuals = data["visuals"].get("visuals", [])
    if len(visuals) == 10:
        results.append("VISUALS: 10 VISUAL PLANS VERIFIED")
    else:
        errors.append(f"VISUALS: EXPECTED 10, FOUND {len(visuals)}")


if data["voice"]:
    voices = data["voice"].get("voice_items", [])
    if len(voices) == 10:
        results.append("VOICE: 10 NARRATION TRACKS VERIFIED")
    else:
        errors.append(f"VOICE: EXPECTED 10, FOUND {len(voices)}")


if data["video"]:
    plans = data["video"].get("production_plans", [])
    if len(plans) == 10:
        results.append("VIDEO: 10 PRODUCTION PLANS VERIFIED")
    else:
        errors.append(f"VIDEO: EXPECTED 10, FOUND {len(plans)}")


if data["editor"]:
    timeline = data["editor"].get("timeline", [])
    if len(timeline) == 10:
        results.append("EDITOR: 10 TIMELINE TRACKS VERIFIED")
    else:
        errors.append(f"EDITOR: EXPECTED 10, FOUND {len(timeline)}")


if data["seo"]:
    youtube = data["seo"].get("youtube", {})
    if youtube.get("title") and youtube.get("description"):
        results.append("SEO: TITLE AND DESCRIPTION VERIFIED")
    else:
        errors.append("SEO: TITLE OR DESCRIPTION MISSING")


if data["master"]:
    if data["master"].get("final_status", {}).get("pipeline_ready") is True:
        results.append("MASTER: PIPELINE READY VERIFIED")
    else:
        errors.append("MASTER: PIPELINE NOT READY")


print("===================================")
print(" FINAL PRODUCTION AUDIT ENGINE")
print("===================================")
print()

for item in results:
    print("[PASS]", item)

for item in errors:
    print("[FAIL]", item)

print()
print("Checks passed:", len(results))
print("Checks failed:", len(errors))
print("FINAL STATUS:", "PRODUCTION READY" if not errors else "REQUIRES FIXES")
