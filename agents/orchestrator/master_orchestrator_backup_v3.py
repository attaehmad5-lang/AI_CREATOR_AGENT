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
        job = self.create_job(request)
        return self.job_engine.run_job(job)

    def create_job(self, request):
        analysis = self.brain.analyze(request)
        job = self.job_engine.create_job(request or {})
        job["brain_analysis"] = analysis
        self.job_engine.save_job(job)
        return job
