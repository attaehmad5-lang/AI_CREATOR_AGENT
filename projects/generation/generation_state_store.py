import json, os, tempfile
from pathlib import Path

class GenerationStateStore:
    def __init__(self, path="projects/generation/generation_state.json"):
        self.path=Path(path)

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix=".generation_state_", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as f:
                json.dump(data,f,indent=4,ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp,self.path)
        except Exception:
            try: os.remove(tmp)
            except OSError: pass
            raise

    def load(self):
        with self.path.open("r",encoding="utf-8") as f:
            return json.load(f)
