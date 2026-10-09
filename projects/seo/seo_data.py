import json
from datetime import datetime

project_name = "World Most Popular Documentary Filming"

seo = {
    "project": project_name,
    "status": "READY",
    "title": "World Most Popular Documentary Filming",
    "description_style": "Cinematic Documentary",
    "keywords": [
        "documentary filming",
        "documentary production",
        "behind the scenes",
        "cinematic documentary",
        "filmmaking",
        "documentary filmmaking",
        "cinematic filming"
    ],
    "tags": [
        "documentary",
        "documentary filming",
        "filmmaking",
        "behind the scenes",
        "cinematic documentary",
        "documentary production"
    ],
    "hashtags": [
        "#Documentary",
        "#Filmmaking",
        "#Cinematic",
        "#BehindTheScenes",
        "#DocumentaryFilming"
    ],
    "publishing": {
        "platform": "YouTube",
        "format": "Long Form Video",
        "aspect_ratio": "16:9",
        "thumbnail": "Cinematic 4K",
        "category": "Documentary"
    },
    "created_at": datetime.now().isoformat()
}

with open("projects/seo/seo.json", "w", encoding="utf-8") as file:
    json.dump(seo, file, indent=4, ensure_ascii=False)

print()
print("===================================")
print("           SEO DATA")
print("===================================")
print()
print("Project:", project_name)
print("SEO file created successfully.")
print()
print("Title:", seo["title"])
print("Keywords:", len(seo["keywords"]))
print("Tags:", len(seo["tags"]))
print("Hashtags:", len(seo["hashtags"]))
print("Platform:", seo["publishing"]["platform"])
print()
print("Saved to: projects/seo/seo.json")
