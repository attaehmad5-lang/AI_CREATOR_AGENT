import json
from datetime import datetime

VIDEO_FILE = "projects/video/video_production_plan.json"
OUTPUT_FILE = "projects/editor/master_edit_plan.json"

with open(VIDEO_FILE, "r", encoding="utf-8") as f:
    video = json.load(f)

tracks = []

for item in video["production_plans"]:
    tracks.append({
        "scene": item["scene"],
        "title": item["title"],
        "duration": item["duration"],
        "visual_track": {
            "source": "AI generated visual footage",
            "direction": item["video_prompt"],
            "camera_motion": item["camera_motion"]
        },
        "narration_track": {
            "text": item["narration"],
            "delivery": item["voice_delivery"],
            "emotion": item["emotion"]
        },
        "music_track": item["music"],
        "sound_effects": item["sound_design"],
        "transition_in": item["transition_in"],
        "transition_out": item["transition_out"],
        "editing_rhythm": item["editing_rhythm"],
        "color_grade": item["color_direction"],
        "audio_mix": item["audio_mix"],
        "status": "READY"
    })

master = {
    "project": video["project"],
    "status": "READY",
    "engine": "Advanced Master Editing Timeline Engine",
    "format": video["format"],
    "resolution": video["resolution"],
    "frame_rate": video["frame_rate"],
    "scene_count": len(tracks),
    "timeline": tracks,
    "global_editing_rules": {
        "visual_continuity": "Maintain consistent subjects, environments, lighting and cinematic language across scenes",
        "audio_continuity": "Keep narration intelligible while preserving natural ambience and cinematic music depth",
        "color_consistency": "Use a unified natural cinematic grade throughout the documentary",
        "transitions": "Prefer motivated cuts, match cuts and restrained dissolves; avoid excessive effects",
        "pacing": "Build gradually from curiosity to emotional engagement and finish with a calm cinematic resolution",
        "quality": "Professional documentary finish, realistic motion, clean audio and no distracting artifacts"
    },
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(master, f, indent=4, ensure_ascii=False)

print("===================================")
print(" ADVANCED MASTER EDITING ENGINE")
print("===================================")
print()
print("Project:", master["project"])
print("Scenes on timeline:", master["scene_count"])
print("Format:", master["format"])
print("Resolution:", master["resolution"])
print("Frame rate:", master["frame_rate"])
print("Status: READY")
print()
print("Master edit plan created successfully.")
print("Saved to:", OUTPUT_FILE)
