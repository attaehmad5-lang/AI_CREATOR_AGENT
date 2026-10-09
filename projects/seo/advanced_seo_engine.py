import json
from datetime import datetime

INPUT_FILE = "projects/seo/seo.json"
OUTPUT_FILE = "projects/seo/advanced_seo_plan.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    seo = json.load(f)

project = seo["project"]

plan = {
    "project": project,
    "status": "READY",
    "engine": "Advanced SEO & Publishing Engine",

    "youtube": {
        "title": "World Most Popular Documentary Filming | Behind The Scenes of Cinematic Filmmaking",
        "description": (
            "Discover the world behind professional documentary filmmaking. "
            "Explore how powerful real-world stories are planned, filmed, recorded, "
            "and transformed into cinematic documentaries through cameras, sound, "
            "lighting, production teams, challenging locations and careful editing."
        ),
        "keywords": [
            "documentary filming",
            "documentary filmmaking",
            "documentary production",
            "behind the scenes",
            "cinematic documentary",
            "filmmaking",
            "cinematic filming",
            "documentary camera",
            "film production",
            "documentary production process"
        ],
        "tags": [
            "documentary",
            "documentary filming",
            "documentary filmmaking",
            "filmmaking",
            "cinematic documentary",
            "behind the scenes",
            "film production",
            "documentary production",
            "cinematic filming",
            "documentary camera"
        ],
        "hashtags": [
            "#Documentary",
            "#Filmmaking",
            "#Cinematic",
            "#BehindTheScenes",
            "#DocumentaryFilming"
        ],
        "category": "Film & Animation",
        "format": "Long Form",
        "aspect_ratio": "16:9",
        "resolution": "4K"
    },

    "thumbnail": {
        "style": "Premium cinematic documentary",
        "aspect_ratio": "16:9",
        "resolution": "4K",
        "direction": "Professional cinematic documentary thumbnail showing a powerful real-world filming environment, professional cinema camera, documentary crew, dramatic natural lighting, realistic subjects, premium film-industry composition, strong depth, high visual contrast, cinematic atmosphere, no watermark."
    },

    "publishing": {
        "platform": "YouTube",
        "content_type": "Long Form Documentary",
        "recommended_format": "4K 24fps",
        "language": "English",
        "content_style": "Cinematic Documentary",
        "metadata_status": "READY"
    },

    "optimization": {
        "search_intent": "Documentary filmmaking and behind-the-scenes production",
        "audience": [
            "Documentary viewers",
            "Filmmaking enthusiasts",
            "Cinema fans",
            "Cinematic content viewers"
        ],
        "discovery_strategy": "Use strong documentary keywords in title, description and tags while maintaining natural language.",
        "click_strategy": "Use a cinematic thumbnail with a clear production story and strong visual subject.",
        "retention_strategy": "Open with a strong visual hook, build curiosity, reveal production challenges and finish with an emotional cinematic conclusion."
    },

    "created_at": datetime.now().isoformat()
}

with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(plan, f, indent=4, ensure_ascii=False)

print("===================================")
print(" ADVANCED SEO & PUBLISHING ENGINE")
print("===================================")
print()
print("Project:", plan["project"])
print("YouTube metadata: READY")
print("Thumbnail strategy: READY")
print("Keywords:", len(plan["youtube"]["keywords"]))
print("Tags:", len(plan["youtube"]["tags"]))
print("Hashtags:", len(plan["youtube"]["hashtags"]))
print("Publishing strategy: READY")
print("Status: READY")
print()
print("Advanced SEO plan created successfully.")
print("Saved to:", OUTPUT_FILE)
