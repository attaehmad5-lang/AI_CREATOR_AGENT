import json

print()
print("===================================")
print("     DOCUMENTARY AI PIPELINE")
print("===================================")
print()

files = [
    ("Research", "projects/research/research.json"),
    ("Story", "projects/story/story.json"),
    ("Scenes", "projects/scenes/scenes.json"),
    ("Visuals", "projects/visuals/visuals.json"),
    ("Voice", "projects/voice/voice.json"),
    ("Video", "projects/video/video.json"),
    ("SEO", "projects/seo/seo.json")
]

for name, path in files:
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    print("[READY]", name)
    print("Project:", data["project"])
    print()

print("===================================")
print("   DOCUMENTARY PIPELINE READY")
print("===================================")
