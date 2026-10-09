from .base_provider import BaseProvider

class TestProvider(BaseProvider):
    name = "Test Provider"

    def generate_image(self, prompt, output_path):
        return {
            "status": "COMPLETED",
            "provider": self.name,
            "output": output_path
        }
