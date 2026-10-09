import requests


class WikidataSource:
    def __init__(self):
        self.status = "READY"
        self.source = "Wikidata"

    def search(self, topic, limit=5):
        topic = (topic or "").strip()

        if not topic:
            return {
                "status": "FAILED",
                "source": self.source,
                "results": []
            }

        params = {
            "action": "wbsearchentities",
            "search": topic,
            "language": "en",
            "format": "json",
            "limit": limit
        }

        response = requests.get(
            "https://www.wikidata.org/w/api.php",
            params=params,
            headers={"User-Agent": "AI_CREATOR_AGENT/1.0"},
            timeout=10
        )
        response.raise_for_status()

        data = response.json()
        results = []

        for item in data.get("search", []):
            entity_id = item.get("id", "")
            label = item.get("label", "")
            description = item.get("description", "")

            if entity_id and label:
                results.append({
                    "id": entity_id,
                    "title": label,
                    "description": description,
                    "url": f"https://www.wikidata.org/wiki/{entity_id}",
                    "source": self.source
                })

        return {
            "status": "READY" if results else "FAILED",
            "source": self.source,
            "topic": topic,
            "results": results
        }
