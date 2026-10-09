import json
from datetime import datetime

research_file = "projects/research/research.json"
story_file = "projects/story/story.json"
output_file = "projects/story/documentary_script.json"

with open(research_file, "r", encoding="utf-8") as file:
    research = json.load(file)

with open(story_file, "r", encoding="utf-8") as file:
    story = json.load(file)

script = {
    "project": story["project"],
    "status": "READY",
    "title": story["title"],
    "genre": story["genre"],
    "target_duration": story["target_duration"],
    "hook": "Behind every great documentary is a hidden world of cameras, people, difficult locations, unexpected moments, and stories that viewers never see.",
    "sections": [
        {"section": 1, "title": "Opening Hook", "narration": "What does it really take to create a documentary that captures the attention of millions?"},
        {"section": 2, "title": "Introduction", "narration": "The journey begins long before the cameras start rolling. Every location, subject, camera angle, and sound must be planned."},
        {"section": 3, "title": "The Main Story", "narration": "At the heart of every powerful documentary is a real story. The filmmakers must discover it, understand it, and bring it to the audience."},
        {"section": 4, "title": "Behind The Scenes", "narration": "Behind the camera, an entire production team works together. Every important moment can change the direction of the story."},
        {"section": 5, "title": "Production Challenges", "narration": "Real locations create real problems. Weather, distance, lighting, sound, equipment, and unexpected events can test the entire production."},
        {"section": 6, "title": "Cinematic Filming", "narration": "Modern documentary filmmaking combines reality with cinematic visual language. Wide landscapes, intimate close-ups, controlled camera movement, and natural light can transform a real moment."},
        {"section": 7, "title": "Human Story", "narration": "Technology may capture the image, but people create the emotion. The strongest documentaries allow viewers to connect with real human experiences."},
        {"section": 8, "title": "The Final Moment", "narration": "After weeks or months of filming, hundreds of hours of material can be reduced to a carefully crafted story."},
        {"section": 9, "title": "Conclusion", "narration": "A great documentary is more than a collection of images. It is a journey built from reality, patience, creativity, and the search for a story worth remembering."},
        {"section": 10, "title": "End Credits", "narration": "This is the world behind documentary filmmaking."}
    ],
    "research_keywords": research["keywords"],
    "created_at": datetime.now().isoformat()
}

with open(output_file, "w", encoding="utf-8") as file:
    json.dump(script, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("     DOCUMENTARY SCRIPT GENERATOR")
print("===================================")
print()
print("Project:", script["project"])
print("Script status:", script["status"])
print("Sections created:", len(script["sections"]))
print("Target duration:", script["target_duration"])
print()
print("Script file created successfully.")
print("Saved to:", output_file)
