import json
from datetime import datetime

SCENE_FILE = "projects/scenes/documentary_scenes.json"
OUTPUT_FILE = "projects/visuals/visual_prompts.json"

with open(SCENE_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

visuals = []

for scene in data["scenes"]:
    visual_prompt = f"Cinematic documentary frame, {scene["visual_direction"]}, location: {scene["location"]}, lighting: {scene["lighting"]}, {scene["camera"]}, realistic human subjects, authentic environment, natural textures, physically accurate lighting, cinematic depth of field, professional documentary cinematography, subtle film grain, high dynamic range, realistic color science, 16:9 composition, 4K detail, no text, no watermark."

    video_prompt = f"Realistic cinematic documentary shot. {scene["visual_direction"]} Camera: {scene["camera"]}. Location: {scene["location"]}. Lighting: {scene["lighting"]}. Natural subject movement, realistic environmental motion, physically accurate camera movement, authentic documentary behavior, smooth cinematic pacing."

    visuals.append({
        "scene": scene["scene"],
        "title": scene["title"],
        "image_prompt": visual_prompt,
        "video_prompt": video_prompt,
        "camera": scene["camera"],
        "lighting": scene["lighting"],
        "location": scene["location"],
        "sound_direction": scene["sound"],
        "duration": scene["duration"],
        "status": "READY"
    })

output = {
    "project": data["project"],
    "status": "READY",
    "engine": "Advanced Visual Prompt Engine",
    "aspect_ratio": "16:9",
    "resolution": "4K",
    "scene_count": len(visuals),
    "visuals": visuals,
    "created_at": datetime.now().isoformat()
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=4, ensure_ascii=False)

print("===================================")
print("   ADVANCED VISUAL PROMPT ENGINE")
print("===================================")
print() 
print("Project:", output["project"])
print("Scenes processed:", output["scene_count"])
print("Image prompts:", len(visuals))
print("Video prompts:", len(visuals))
print("Status: READY")
print()
print("Visual prompt file created successfully.")
print("Saved to:", OUTPUT_FILE)
