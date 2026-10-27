"""Build, install-smoke-test, and reproducibility-check the provider-store wheel."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return completed.stdout


def verify_wheel(repo_root: Path | None = None) -> dict[str, object]:
    root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    if not (root / "pyproject.toml").is_file():
        raise ValueError("root pyproject.toml is missing")
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = "1704067200"
    env["PYTHONHASHSEED"] = "0"
    with tempfile.TemporaryDirectory(prefix="provider-store-wheel-") as tmp:
        work = Path(tmp)
        wheel_dirs = [work / "build-a", work / "build-b"]
        wheels: list[Path] = []
        for wheel_dir in wheel_dirs:
            wheel_dir.mkdir()
            _run(
                [
                    sys.executable, "-m", "pip", "wheel", "--no-deps",
                    "--wheel-dir", str(wheel_dir), str(root),
                ],
                cwd=work,
                env=env,
            )
            candidates = list(wheel_dir.glob("*.whl"))
            if len(candidates) != 1:
                raise ValueError("wheel build did not produce exactly one wheel")
            wheels.append(candidates[0])
        digests = [
            hashlib.sha256(path.read_bytes()).hexdigest() for path in wheels
        ]
        if digests[0] != digests[1]:
            raise ValueError("two isolated wheel builds were not byte-for-byte reproducible")

        target = work / "installed-wheel"
        _run(
            [
                sys.executable, "-m", "pip", "install", "--no-deps",
                "--target", str(target), str(wheels[0]),
            ],
            cwd=work,
            env=env,
        )
        smoke_env = env.copy()
        smoke_env["PYTHONPATH"] = str(target)
        smoke_code = (
            "import importlib.resources; "
            "import projects.provider_store.catalog_cli; "
            "resource = importlib.resources.files('projects.provider_store').joinpath('provider_catalog.json'); "
            "assert resource.is_file(), 'provider catalog JSON missing from wheel'; "
            "print('WHEEL_IMPORT_AND_DATA_PASS')"
        )
        output = _run(
            [sys.executable, "-c", smoke_code],
            cwd=work,
            env=smoke_env,
        )
        if "WHEEL_IMPORT_AND_DATA_PASS" not in output:
            raise ValueError("installed wheel smoke test did not report success")
        return {
            "status": "PASS",
            "wheel": wheels[0].name,
            "sha256": digests[0],
            "reproducible": True,
            "installed_import": True,
            "catalog_data_included": True,
            "runtime_dependencies": [],
            "build_dependencies": ["setuptools==80.9.0", "wheel==0.45.1"],
        }


def main() -> int:
    try:
        result = verify_wheel()
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
