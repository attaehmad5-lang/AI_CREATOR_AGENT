import json
from datetime import datetime

SCENE_FILE = "projects/scenes/documentary_scenes.json"
VISUAL_FILE = "projects/visuals/visual_prompts.json"
VOICE_FILE = "projects/voice/voice_prompts.json"
OUTPUT_FILE = "projects/video/video_production_plan.json"

with open(SCENE_FILE, "r", encoding="utf-8") as f:
    scenes = json.load(f)
with open(VISUAL_FILE, "r", encoding="utf-8") as f:
    visuals = json.load(f)
with open(VOICE_FILE, "r", encoding="utf-8") as f:
    voices = json.load(f)

plans = []

for i, scene in enumerate(scenes["scenes"]):
    visual = visuals["visuals"][i]
    voice = voices["voice_items"][i]

    plans.append({
        "scene": scene["scene"],
        "title": scene["title"],
        "duration": scene["duration"],
        "shot_type": "Cinematic documentary coverage with establishing, medium, close-up and detail shots",
        "camera_motion": "Slow controlled movement, motivated tracking, subtle push-ins and stable observational framing",
        "image_prompt": visual["image_prompt"],
        "video_prompt": visual["video_prompt"],
        "narration": voice["narration"],
        "voice_delivery": voice["delivery"],
        "emotion": voice["emotion"],
        "music": voice["music_direction"],
        "sound_design": scene["sound"],
        "transition_in": "Clean cinematic cut or soft dissolve based on story rhythm",
        "transition_out": "Natural visual transition into the following scene",
        "editing_rhythm": "Controlled pacing with longer holds for atmosphere and faster cuts during active moments",
        "continuity": "Maintain consistent subject appearance, environment, lighting logic, camera language and documentary realism",
        "color_direction": "Natural cinematic color grade, realistic skin tones, controlled highlights, subtle film texture",
        "audio_mix": "Narration clear and forward, music underneath, natural ambience preserved, no harsh clipping",
        "status": "READY"
    })

output = {
    "project": scenes["project"],
    "status": "READY",
    "engine": "Advanced Video Production Engine",
    "format": "16:9",
    "resolution": "4K",
    "frame_rate": "24fps",
    "scene_count": len(plans),
    "production_plans": plans,
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print(" ADVANCED VIDEO PRODUCTION ENGINE")
print("===================================")
print()
print("Project:", output["project"])
print("Scenes processed:", output["scene_count"])
print("Production plans:", len(plans))
print("Format:", output["format"])
print("Resolution:", output["resolution"])
print("Frame rate:", output["frame_rate"])
print("Status: READY")
print()
print("Video production plan created successfully.")
print("Saved to:", OUTPUT_FILE)
