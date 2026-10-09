import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class GenerationStateBackup:
    def __init__(
        self,
        state_path="projects/generation/generation_state.json",
        backup_dir="projects/generation/state_backups",
    ):
        self.state_path = Path(state_path)
        self.backup_dir = Path(backup_dir)

    @staticmethod
    def _validate_json(path):
        with Path(path).open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError("State data must be a JSON object")

        if not isinstance(data.get("states"), list):
            raise ValueError("State data must contain a states list")

        return data

    def create_backup(self):
        self._validate_json(self.state_path)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.backup_dir / f"generation_state_{timestamp}.json"

        with self.state_path.open("rb") as source:
            fd, temporary = tempfile.mkstemp(
                prefix=".state_backup_",
                suffix=".tmp",
                dir=str(self.backup_dir),
            )
            try:
                with os.fdopen(fd, "wb") as target:
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())

                self._validate_json(temporary)
                os.replace(temporary, destination)
            except Exception:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
                raise

        return str(destination)

    def restore_backup(self, backup_path):
        backup = Path(backup_path).resolve()
        backup_root = self.backup_dir.resolve()

        if not backup.is_file():
            raise FileNotFoundError("Backup file does not exist")

        if backup.parent != backup_root:
            raise ValueError("Backup must be inside the approved backup directory")

        self._validate_json(backup)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

        fd, temporary = tempfile.mkstemp(
            prefix=".generation_state_restore_",
            suffix=".tmp",
            dir=str(self.state_path.parent),
        )
        try:
            with backup.open("rb") as source, os.fdopen(fd, "wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())

            self._validate_json(temporary)
            os.replace(temporary, self.state_path)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise

        return True
