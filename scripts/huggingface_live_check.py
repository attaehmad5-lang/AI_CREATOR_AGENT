"""Safely verify Hugging Face credentials and optionally generate one test image.

Never prints or writes the access token. Image generation is opt-in because the
provider/model may consume account credits or have usage limits.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from urllib.request import Request

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.http_probe import ProviderPinnedHTTPSOpener


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def verify_token(token: str) -> None:
    endpoint = "https://huggingface.co/api/whoami-v2"
    validation = ConnectionManager.validate_endpoint(
        endpoint, allowed_hosts={"huggingface.co"},
    )
    if validation.get("valid") is not True:
        raise RuntimeError("Hugging Face identity endpoint failed host-policy validation")
    request = Request(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
        method="GET",
    )
    response = None
    try:
        response = ProviderPinnedHTTPSOpener().open(request, timeout=15)
        status = response.getcode()
        if status in (401, 403):
            raise RuntimeError("Hugging Face rejected the token (unauthorized/forbidden)")
        if 300 <= status < 400:
            raise RuntimeError("Hugging Face identity check redirect was rejected")
        if status != 200:
            raise RuntimeError(f"Hugging Face identity check returned HTTP {status}")
        body = response.read(64 * 1024 + 1)
        if not isinstance(body, bytes) or len(body) > 64 * 1024:
            raise RuntimeError("Hugging Face identity response exceeded the size limit")
    except TimeoutError:
        raise RuntimeError("Hugging Face network check timed out") from None
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Hugging Face network check failed: {type(exc).__name__}") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def main() -> int:
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        print("FAIL: HF_TOKEN secret is missing or empty.")
        return 2

    try:
        verify_token(token)
    except RuntimeError as exc:
        print(f"FAIL: {exc}")
        return 1

    print("PASS: Hugging Face accepted the token. Token value was not displayed.")

    if os.environ.get("HF_LIVE_GENERATION", "").casefold() != "true":
        print("SKIP: Image generation was not requested; no model inference was made.")
        return 0

    try:
        image_module = importlib.import_module("PIL.Image")
        provider_module = importlib.import_module(
            "projects.generation.providers.huggingface_provider"
        )
        provider_class = getattr(provider_module, "HuggingFaceProvider")

        output = REPOSITORY_ROOT / "output" / "huggingface-live-test.png"
        result = provider_class().generate_image(
            "A cinematic realistic landscape photograph at golden hour, natural colors, no text",
            str(output),
        )
        if result.get("status") != "COMPLETED" or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("provider did not produce a non-empty image file")
        with image_module.open(output) as image:
            image.verify()
        print("PASS: Hugging Face image generation returned a valid image file.")
        print(f"Image artifact: {output.relative_to(REPOSITORY_ROOT)} ({output.stat().st_size} bytes)")
    except ModuleNotFoundError as exc:
        missing = exc.name or "unknown"
        print(f"FAIL: required Python module is missing: {missing}")
        return 1
    except Exception as exc:
        # Exception text may contain request data; expose only the exception class.
        print(f"FAIL: image generation failed ({type(exc).__name__}). Check model access, provider availability, and account credits.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
