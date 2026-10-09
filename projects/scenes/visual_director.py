import json

input_file = "projects/scenes/documentary_scenes.json"
output_file = "projects/scenes/cinematic_scenes.json"

with open(input_file, "r", encoding="utf-8") as file:
    data = json.load(file)

visual_plan = {
    1: {
        "visual_direction": "Massive cinematic documentary opening with a professional film crew preparing cameras before sunrise.",
        "camera": "Slow aerial establishing shot, then 35mm cinematic push-in.",
        "location": "Remote natural landscape and documentary production base.",
        "lighting": "Blue-hour atmosphere transitioning into warm sunrise.",
        "sound": "Deep cinematic drone, wind, distant natural ambience.",
        "duration": "00:00-02:00"
    },
    2: {
        "visual_direction": "Introduce the documentary world through locations, equipment, maps, crew preparation and subjects.",
        "camera": "Wide establishing shots, handheld documentary coverage and controlled close-ups.",
        "location": "Production office, equipment area and filming location.",
        "lighting": "Natural daylight with soft cinematic contrast.",
        "sound": "Natural location sound, camera equipment and subtle ambient music.",
        "duration": "02:00-04:00"
    },
    3: {
        "visual_direction": "Reveal the central documentary story through authentic human moments and powerful environmental imagery.",
        "camera": "35mm medium shots, 85mm emotional close-ups and slow tracking shots.",
        "location": "Primary documentary subject location.",
        "lighting": "Natural available light with carefully controlled highlights.",
        "sound": "Natural ambience, dialogue fragments and restrained cinematic score.",
        "duration": "04:00-08:00"
    },
    4: {
        "visual_direction": "Show the hidden production world: camera operators, sound crew, directors, monitors and real filming activity.",
        "camera": "Handheld behind-the-scenes shots mixed with smooth tracking movements.",
        "location": "Active documentary production set.",
        "lighting": "Real production lighting mixed with natural environmental light.",
        "sound": "Crew communication, camera operation sounds and production ambience.",
        "duration": "08:00-11:00"
    },
    5: {
        "visual_direction": "Build tension through difficult weather, challenging terrain, equipment problems and unpredictable filming conditions.",
        "camera": "Dynamic handheld movement, wide environmental shots and tight detail close-ups.",
        "location": "Difficult outdoor filming environment.",
        "lighting": "Moody natural light with dramatic clouds and high contrast.",
        "sound": "Strong environmental ambience, wind, equipment sounds and tense music.",
        "duration": "11:00-14:00"
    },
    6: {
        "visual_direction": "Demonstrate premium cinematic filmmaking using landscapes, controlled movement, lenses and natural light.",
        "camera": "Drone aerials, slow dolly movement, 35mm wides and 85mm close-ups.",
        "location": "Large-scale cinematic natural environment.",
        "lighting": "Golden hour, soft directional sunlight and cinematic shadows.",
        "sound": "Atmospheric score with subtle environmental sound.",
        "duration": "14:00-17:00"
    },
    7: {
        "visual_direction": "Focus on the human side of filmmaking through intimate faces, emotions and authentic interactions.",
        "camera": "85mm portrait close-ups with shallow depth of field and slow controlled movement.",
        "location": "Real home, community or personal environment.",
        "lighting": "Soft natural window light with gentle cinematic fill.",
        "sound": "Quiet room tone, natural dialogue and emotional piano.",
        "duration": "17:00-19:00"
    },
    8: {
        "visual_direction": "Create a powerful final sequence showing the culmination of months of filming and storytelling.",
        "camera": "Slow tracking shot followed by wide cinematic reveal and intimate close-up.",
        "location": "Final documentary location.",
        "lighting": "Late-afternoon warm light transitioning toward dusk.",
        "sound": "Emotional strings, natural ambience and controlled silence.",
        "duration": "19:00-21:00"
    },
    9: {
        "visual_direction": "Deliver the final message with memorable landscapes, filmmakers and authentic documentary moments.",
        "camera": "Wide master shots, slow crane movement and final static composition.",
        "location": "Signature documentary landscape.",
        "lighting": "Warm cinematic sunset light.",
        "sound": "Emotional cinematic score gradually fading into natural ambience.",
        "duration": "21:00-23:00"
    },
    10: {
        "visual_direction": "Professional closing sequence showing the crew, cameras and final landscape before credits.",
        "camera": "Slow cinematic pull-back into a wide final frame.",
        "location": "Documentary production location.",
        "lighting": "Soft twilight and practical production lights.",
        "sound": "Minimal cinematic outro and natural environmental sound.",
        "duration": "23:00-24:00"
    }
}

for scene in data["scenes"]:
    plan = visual_plan[scene["scene"]]
    scene.update(plan)

data["status"] = "CINEMATIC_READY"

with open(output_file, "w", encoding="utf-8") as file:
    json.dump(data, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("       VISUAL DIRECTOR")
print("===================================")
print()
print("Project:", data["project"])
print("Scenes processed:", len(data["scenes"]))
print("Status:", data["status"])
print()
print("Cinematic scene data created successfully.")
print("Saved to:", output_file)
