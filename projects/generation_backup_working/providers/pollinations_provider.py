import requests
from pathlib import Path

class PollinationsProvider:
    def generate_image(self, prompt, output_path):
        url = "https://image.pollinations.ai/prompt/" + requests.utils.quote(prompt)
        response = requests.get(url, timeout=300)
        if response.status_code != 200:
            raise RuntimeError(f"Pollinations HTTP {response.status_code}: {response.text[:500]}")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(response.content)
        return {"status": "COMPLETED", "provider": "Pollinations", "output": output_path}
