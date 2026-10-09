import json

input_file = "projects/scenes/documentary_scenes.json"

with open(input_file, "r", encoding="utf-8") as f:
    data = json.load(f)

scene_details = [
    ("Cinematic aerial establishing shots of a vast real-world location, introducing the scale and mystery of the documentary.", "24mm wide lens, slow aerial drone movement, gentle push-in", "Real-world exterior location", "Natural dawn light with soft atmospheric haze", "Wind, distant environmental ambience, subtle cinematic drone"),
    ("Professional documentary crew preparing cameras, microphones and equipment before filming begins.", "35mm lens, slow tracking shots, detailed close-ups", "Documentary production location", "Soft natural morning light with practical fill", "Equipment handling, footsteps, quiet crew communication"),
    ("The main documentary subject moving naturally through an authentic environment while the camera observes without disturbing the moment.", "50mm lens, handheld documentary movement, intimate close-ups", "Authentic real-world environment", "Natural daylight with realistic contrast", "Natural location sound, footsteps, environmental ambience"),
    ("Behind-the-scenes view of the film crew working together while cameras capture important moments.", "35mm lens, shoulder-mounted camera, observational framing", "Active filming location", "Mixed natural and practical production lighting", "Crew communication, camera operation, ambient location sound"),
    ("The production team facing difficult weather, terrain and logistical conditions while continuing to film.", "24mm and 50mm lenses, handheld movement, occasional static frames", "Remote or challenging location", "Overcast natural light with dramatic atmospheric conditions", "Wind, rain or environmental sounds, equipment movement"),
    ("Highly cinematic documentary imagery showing landscapes, details and human moments with controlled visual composition.", "24mm wide shots, 85mm close-ups, slow dolly and tracking movement", "Cinematic real-world location", "Golden-hour sunlight and soft cinematic highlights", "Atmospheric music, natural ambience, subtle sound design"),
    ("An intimate human moment revealing emotion, personality and the real story behind the documentary subject.", "85mm portrait lens, shallow depth of field, slow controlled push-in", "Quiet authentic environment", "Soft diffused natural light", "Breathing, subtle environmental ambience, restrained music"),
    ("Final major filming moment as the crew captures a powerful image that completes the central story.", "35mm cinematic lens, slow crane or dolly movement", "Key documentary location", "Late-afternoon dramatic natural light", "Rising cinematic score, natural environmental sound"),
    ("A wide reflective view summarizing the journey, returning to the environment where the story began.", "24mm wide lens, slow pull-back, stable composition", "Original documentary environment", "Warm sunset light with long shadows", "Soft piano, ambient atmosphere, gentle wind"),
    ("Elegant closing images of the location and production team, creating a professional documentary ending.", "35mm lens, slow cinematic fade-out movement", "Documentary location", "Soft twilight light", "Final musical resolve, natural ambience, clean ending"),
]

for i, scene in enumerate(data["scenes"]):
    details = scene_details[i]
    scene["visual_direction"] = details[0]
    scene["camera"] = details[1]
    scene["location"] = details[2]
    scene["lighting"] = details[3]
    scene["sound"] = details[4]
    scene["duration"] = "02:00" if i < 3 else "03:00"
    scene["status"] = "READY"

data["status"] = "READY"

with open(input_file, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=4, ensure_ascii=False)

print("CINEMATIC SCENE BUILDER")
print("Project:", data["project"])
print("Scenes updated:", len(data["scenes"]))
print("Status: READY")
print("Cinematic details added successfully.")
