class SEOAgent:
    def __init__(self):
        self.name = "SEO Agent"

    def plan_seo(self, topic):
        print()
        print("[SEO AGENT]")
        print("Topic:", topic)
        print("SEO status: READY")
        print("Researching search keywords...")
        print("Planning YouTube title...")
        print("Planning video description...")
        print("Planning tags and hashtags...")
        print("Planning publishing strategy...")
        print("SEO planning complete.")


if __name__ == "__main__":
    agent = SEOAgent()
    agent.plan_seo("YouTube Music Channel")
