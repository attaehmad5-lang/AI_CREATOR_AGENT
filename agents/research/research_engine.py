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

    def match_claims(self, claims):
        claims = claims if isinstance(claims, list) else []
        groups = []
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            text = ' '.join(str(claim.get('claim', '')).lower().split())
            words = {w.strip(' ,;:()[]{}"\'') for w in text.split() if len(w.strip(' ,;:()[]{}"\'')) >= 5}
            if not words:
                continue
            matches = []
            for other_index, other in enumerate(claims):
                if index == other_index or not isinstance(other, dict):
                    continue
                if claim.get('source') == other.get('source'):
                    continue
                other_text = ' '.join(str(other.get('claim', '')).lower().split())
                other_words = {w.strip(' ,;:()[]{}"\'') for w in other_text.split() if len(w.strip(' ,;:()[]{}"\'')) >= 5}
                overlap = words.intersection(other_words)
                if len(overlap) >= 3:
                    matches.append({'claim_index': other_index, 'source': other.get('source', ''), 'title': other.get('title', ''), 'url': other.get('url', ''), 'shared_terms': sorted(overlap)})
            if matches:
                groups.append({'claim_index': index, 'source': claim.get('source', ''), 'matches': matches[:5]})
        return {'status': 'READY' if groups else 'FAILED', 'matches': groups, 'match_count': len(groups)}

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

    def classify_claim_matches(self, match_result):
        match_result = match_result if isinstance(match_result, dict) else {}
        groups = match_result.get('matches', [])
        classifications = []
        for group in groups:
            matches = group.get('matches', []) if isinstance(group, dict) else []
            if not matches:
                continue
            strongest = max((len(item.get('shared_terms', [])) for item in matches if isinstance(item, dict)), default=0)
            if strongest >= 5:
                classification = 'SUPPORTED'
            elif strongest >= 3:
                classification = 'UNCERTAIN'
            else:
                classification = 'UNCERTAIN'
            classifications.append({'claim_index': group.get('claim_index'), 'source': group.get('source', ''), 'classification': classification, 'evidence_count': len(matches), 'strongest_overlap': strongest})
        return {'status': 'READY' if classifications else 'FAILED', 'classifications': classifications, 'classification_count': len(classifications)}

    def detect_claim_conflicts(self, claims):
        claims = claims if isinstance(claims, list) else []
        conflicts = []
        negative_terms = {"not", "never", "no", "cannot", "cannot", "false", "untrue", "impossible"}
        for index, claim in enumerate(claims):
            if not isinstance(claim, dict):
                continue
            text = ' '.join(str(claim.get('claim', '')).lower().split())
            words = set(text.split())
            for other_index in range(index + 1, len(claims)):
                other = claims[other_index]
                if not isinstance(other, dict) or claim.get('source') == other.get('source'):
                    continue
                other_text = ' '.join(str(other.get('claim', '')).lower().split())
                other_words = set(other_text.split())
                shared = words.intersection(other_words)
                if len(shared) < 5:
                    continue
                first_negative = bool(words.intersection(negative_terms))
                second_negative = bool(other_words.intersection(negative_terms))
                if first_negative != second_negative:
                    conflicts.append({'claim_index': index, 'other_claim_index': other_index, 'source': claim.get('source', ''), 'other_source': other.get('source', ''), 'shared_terms': sorted(shared)})
        return {'status': 'READY', 'conflicts': conflicts, 'conflict_count': len(conflicts)}

    def classify_conflicts(self, conflict_result):
        conflict_result = conflict_result if isinstance(conflict_result, dict) else {}
        conflicts = conflict_result.get('conflicts', [])
        if conflicts:
            return {'status': 'READY', 'classification': 'CONFLICT', 'conflict_count': len(conflicts)}
        return {'status': 'READY', 'classification': 'NO_CONFLICT_EVIDENCE', 'conflict_count': 0}

    def calculate_claim_confidence(self, claim, evidence_count=0, conflict=False):
        if not isinstance(claim, dict) or not str(claim.get('claim', '')).strip():
            return {'status': 'FAILED', 'confidence': 0, 'level': 'INSUFFICIENT_EVIDENCE'}
        evidence_count = max(0, int(evidence_count or 0))
        if conflict:
            return {'status': 'READY', 'confidence': 25, 'level': 'CONFLICTING_EVIDENCE'}
        if evidence_count >= 2:
            return {'status': 'READY', 'confidence': 80, 'level': 'HIGH'}
        if evidence_count == 1:
            return {'status': 'READY', 'confidence': 55, 'level': 'MEDIUM'}
        return {'status': 'READY', 'confidence': 20, 'level': 'LOW'}

    def build_claim_confidence(self, claims, conflict_result):
        claims = claims if isinstance(claims, list) else []
        conflict_result = conflict_result if isinstance(conflict_result, dict) else {}
        conflicts = conflict_result.get('conflicts', [])
        confidence_results = []
        for index, claim in enumerate(claims):
            evidence_count = 0
            conflict = False
            for item in conflicts:
                if not isinstance(item, dict):
                    continue
                if item.get('claim_index') == index or item.get('other_claim_index') == index:
                    conflict = True
                    evidence_count += 1
            result = self.calculate_claim_confidence(claim, evidence_count, conflict)
            result['claim_index'] = index
            confidence_results.append(result)
        return {'status': 'READY' if confidence_results else 'FAILED', 'confidence_results': confidence_results, 'confidence_count': len(confidence_results)}

    def score_source_reliability(self, source):
        if not isinstance(source, dict):
            return {'status': 'FAILED', 'source': '', 'score': 0, 'level': 'UNKNOWN'}
        name = str(source.get('source', '')).strip().lower()
        if name == 'wikidata':
            return {'status': 'READY', 'source': source.get('source', ''), 'score': 85, 'level': 'HIGH'}
        if name == 'wikipedia':
            return {'status': 'READY', 'source': source.get('source', ''), 'score': 75, 'level': 'MEDIUM_HIGH'}
        if name:
            return {'status': 'READY', 'source': source.get('source', ''), 'score': 50, 'level': 'MEDIUM'}
        return {'status': 'READY', 'source': '', 'score': 0, 'level': 'UNKNOWN'}

    def analyze(self, request):
        request = request or {}
        mode = request.get("mode", "research")
        topic = request.get("topic", "")

        source_result = self.collect_sources(topic)
        sources = source_result.get("sources", [])

        reference_result = self.validate_references(sources)
        claim_result = self.extract_claims(sources)
        claims = claim_result.get("claims", [])
        match_result = self.match_claims(claims)
        classification_result = self.classify_claim_matches(match_result)
        conflict_result = self.detect_claim_conflicts(claims)
        conflict_classification = self.classify_conflicts(conflict_result)
        confidence_result = self.build_claim_confidence(claims, conflict_result)

        return {
            "status": "READY" if source_result["status"] == "READY" and reference_result["validated"] else "FAILED",
            "mode": mode,
            "topic": topic,
            "source_collection": bool(sources),
            "fact_checking": mode == "fact_checking",
            "reference_validation": reference_result["validated"],
            "sources": sources,
            "references": sources,
            "claims": claims,
            "claim_matches": match_result.get("matches", []),
            "claim_classifications": classification_result.get("classifications", []),
            "claim_conflicts": conflict_result.get("conflicts", []),
            "conflict_count": conflict_result.get("conflict_count", 0),
            "conflict_classification": conflict_classification.get("classification", "UNCERTAIN"),
            "claim_confidence": confidence_result.get("confidence_results", []),
            "confidence_count": confidence_result.get("confidence_count", 0),
            "research_status": "READY" if sources else "FAILED"
        }
