import json
from datetime import datetime

project_name = "World Most Popular Documentary Filming"

video = {
    "project": project_name,
    "status": "READY",
    "video_style": "Cinematic Documentary",
    "aspect_ratio": "16:9",
    "resolution": "4K",
    "frame_rate": "24fps",
    "camera_movement": [
        "Slow cinematic movement",
        "Handheld documentary shots",
        "Smooth tracking shots",
        "Drone aerial movement",
        "Static interview shots"
    ],
    "transitions": [
        "Clean cuts",
        "Cinematic fades",
        "Slow dissolves",
        "Match cuts"
    ],
    "audio": [
        "Narration",
        "Ambient sound",
        "Cinematic music",
        "Natural location sound"
    ],
    "created_at": datetime.now().isoformat()
}

with open("projects/video/video.json", "w", encoding="utf-8") as file:
    json.dump(video, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("          VIDEO DATA")
print("===================================")
print()
print("Project:", project_name)
print("Video file created successfully.")
print()
print("Video style:", video["video_style"])
print("Aspect ratio:", video["aspect_ratio"])
print("Resolution:", video["resolution"])
print("Frame rate:", video["frame_rate"])
print("Camera movements:", len(video["camera_movement"]))
print("Transitions:", len(video["transitions"]))
print("Audio elements:", len(video["audio"]))
print()
print("Saved to: projects/video/video.json")
