from agents.research.web_source import WebSource
from agents.research.wikidata_source import WikidataSource


class ResearchEngine:
    def __init__(self):
        self.status = "READY"
        self.version = "1.1"
        self.modes = [
            "research",
            "fact_checking",
            "source_collection",
            "reference_validation"
        ]
        self.web_source = WebSource()
        self.wikidata_source = WikidataSource()

    def collect_sources(self, topic):
        topic = (topic or "").strip()

        if not topic:
            return {
                "status": "FAILED",
                "topic": topic,
                "sources": [],
                "source_collection_status": "FAILED"
            }

        try:
            web_result = self.web_source.search(topic, 5)
            wikidata_result = self.wikidata_source.search(topic, 5)
            sources = web_result.get("results", []) + wikidata_result.get("results", [])

            return {
                "status": "READY" if sources else "FAILED",
                "topic": topic,
                "sources": sources,
                "source_collection_status": "READY" if sources else "FAILED"
            }

        except Exception as e:
            return {
                "status": "FAILED",
                "topic": topic,
                "sources": [],
                "source_collection_status": "FAILED",
                "error": str(e)
            }

    def extract_claims(self, sources):
        sources = sources if isinstance(sources, list) else []
        claims = []

        for source in sources:
            if not isinstance(source, dict):
                continue

            text = source.get("content", "") or source.get("description", "")
            text = " ".join(str(text).split())

            if not text:
                continue

            sentences = [item.strip() for item in text.replace("!", ".").replace("?", ".").split(".") if len(item.strip()) >= 40]
            chunks = []
            for sentence in sentences:
                words = sentence.split()
                if len(words) >= 8:
                    chunks.append(" ".join(words[:80]))
                if len(chunks) >= 20:
                    break

            for claim_text in chunks:
                claims.append({
                    "claim": claim_text,
                    "source": source.get("source", ""),
                    "title": source.get("title", ""),
                    "url": source.get("url", "")
                })

        return {
            "status": "READY" if claims else "FAILED",
            "claims": claims,
            "claim_count": len(claims)
        }

    def validate_references(self, references):
        references = references if isinstance(references, list) else []

        valid = True
        for reference in references:
            if not isinstance(reference, dict):
                valid = False
                break

            if not reference.get("title"):
                valid = False
                break

            if not reference.get("url"):
                valid = False
                break

            if not reference.get("source"):
                valid = False
                break

        return {
            "status": "READY" if valid else "FAILED",
            "references": references,
            "validated": valid,
            "reference_validation_status": "READY" if valid else "FAILED"
        }

    def analyze(self, request):
        request = request or {}
        mode = request.get("mode", "research")
        topic = request.get("topic", "")

        source_result = self.collect_sources(topic)
        sources = source_result.get("sources", [])

        reference_result = self.validate_references(sources)

        return {
            "status": "READY" if source_result["status"] == "READY" and reference_result["validated"] else "FAILED",
            "mode": mode,
            "topic": topic,
            "source_collection": bool(sources),
            "fact_checking": mode == "fact_checking",
            "reference_validation": reference_result["validated"],
            "sources": sources,
            "references": sources,
            "research_status": "READY" if sources else "FAILED"
        }
