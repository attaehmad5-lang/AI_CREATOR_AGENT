import os

project_name = "World Most Popular Documentary Filming"

folders = [
    "research",
    "story",
    "scenes",
    "visuals",
    "voice",
    "video",
    "seo"
]

print()
print("===================================")
print("      DOCUMENTARY PROJECT")
print("===================================")
print()
print("Project:", project_name)
print()

for folder in folders:
    path = os.path.join("projects", folder)
    os.makedirs(path, exist_ok=True)
    print("[READY]", path)

print()
print("Documentary production structure created successfully.")
