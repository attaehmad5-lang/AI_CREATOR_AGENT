import re
import html
import requests
from bs4 import BeautifulSoup


class WebSource:
    def __init__(self):
        self.status = "READY"
        self.source = "Wikipedia"

    def fetch_content(self, url):
        response = requests.get(url, headers={"User-Agent": "AI_CREATOR_AGENT/1.0"}, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for item in soup(["script", "style", "noscript"]):
            item.decompose()
        text = " ".join(soup.stripped_strings)
        return {"status": "READY", "url": url, "text": text}

    def search(self, topic, limit=5):
        topic = (topic or "").strip()
        if not topic:
            return {"status": "FAILED", "source": self.source, "results": []}

        params = {
            "action": "query",
            "list": "search",
            "srsearch": topic,
            "format": "json",
            "srlimit": limit
        }

        response = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params=params,
            headers={"User-Agent": "AI_CREATOR_AGENT/1.0"},
            timeout=10
        )
        response.raise_for_status()

        data = response.json()
        results = []

        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "").strip()
            snippet = re.sub(
                r"<[^>]+>",
                "",
                html.unescape(item.get("snippet", ""))
            ).strip()

            if title:
                results.append({
                    "title": title,
                    "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
                    "snippet": snippet,
                    "source": self.source
                })

        return {
            "status": "READY",
            "source": self.source,
            "topic": topic,
            "results": results
        }
