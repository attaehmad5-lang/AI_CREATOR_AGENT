from agents.research.research_agent import ResearchAgent
from agents.script.script_agent import ScriptAgent
from agents.visual.visual_agent import VisualAgent
from agents.voice.voice_agent import VoiceAgent
from agents.video.video_agent import VideoAgent

class CreatorBrain:
    def __init__(self):
        self.name = "AI Creator Brain"
        self.goal = "Create high quality YouTube content"

        self.research_agent = ResearchAgent()
        self.script_agent = ScriptAgent()
        self.visual_agent = VisualAgent()
        self.voice_agent = VoiceAgent()
        self.video_agent = VideoAgent()

    def think(self, topic):
        print()
        print("[CREATOR BRAIN]")
        print("Topic:", topic)

        print()
        print("=== RESEARCH PHASE ===")
        self.research_agent.research(topic)

        print()
        print("=== SCRIPT PHASE ===")
        self.script_agent.create_script(topic)

        print()
        print("=== VISUAL PHASE ===")
        self.visual_agent.plan_visuals(topic)

        print()
        print("=== VOICE PHASE ===")
        self.voice_agent.plan_voice(topic)

        print()
        print("=== VIDEO PHASE ===")
        self.video_agent.plan_video(topic)

        print()
        print("=== CREATOR PIPELINE ===")
        print("[1] Research complete")
        print("[2] Script complete")
        print("[3] Visual planning complete")
        print("[4] Voice planning complete")
        print("[5] Video planning complete")
        print("[6] Prepare SEO and publishing")
        print("[7] Analyze performance and improve")

    def run(self, topic):
        self.think(topic)

if __name__ == "__main__":
    brain = CreatorBrain()
    brain.run("YouTube Music Channel")
