class ResearchAgent:
    def __init__(self):
        self.name = "Research Agent"

    def research(self, topic):
        print()
        print("[RESEARCH AGENT]")
        print("Topic:", topic)
        print("Research status: READY")
        print("Finding audience interest...")
        print("Finding content angles...")
        print("Finding important keywords...")
        print("Research complete.")

if __name__ == "__main__":
    agent = ResearchAgent()
    agent.research("YouTube Music Channel")
