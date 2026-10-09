from agents.brain.creative_brain import CreativeBrain
from agents.jobs.job_engine import MasterJobEngine
from agents.research.research_engine import ResearchEngine

class MasterOrchestrator:
    def __init__(self):
        self.brain = CreativeBrain()
        self.job_engine = MasterJobEngine()
        self.research_engine = ResearchEngine()
        self.status = "READY"
        self.version = "1.0"

    def run(self, request):
        request = request or {}
        analysis = self.brain.analyze(request)
        if analysis.get("content_type") in ["research", "fact_checking"]:
            mode = "fact_checking" if analysis.get("content_type") == "fact_checking" else "research"
            topic = request.get("topic", analysis.get("topic", ""))
            research_result = self.research_engine.analyze({"mode": mode, "topic": topic})
            source_result = self.research_engine.collect_sources(topic)
            reference_result = self.research_engine.validate_references(request.get("references", []))
            job = self.create_job(request)
            job["research_result"] = research_result
            job["source_result"] = source_result
            job["reference_result"] = reference_result
            self.job_engine.save_job(job)
            return job
        job = self.create_job(request)
        return self.job_engine.run_job(job)

    def create_job(self, request):
        analysis = self.brain.analyze(request)
        job = self.job_engine.create_job(request or {})
        job["brain_analysis"] = analysis
        self.job_engine.save_job(job)
        return job
