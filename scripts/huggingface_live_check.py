"""Safely verify Hugging Face credentials and optionally generate one test image.

Never prints or writes the access token. Image generation is opt-in because the
provider/model may consume account credits or have usage limits.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def verify_token(token: str) -> None:
    request = urllib.request.Request(
        "https://huggingface.co/api/whoami-v2",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 200:
                raise RuntimeError(f"Hugging Face identity check returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("Hugging Face rejected the token (unauthorized/forbidden)") from None
        raise RuntimeError(f"Hugging Face identity check returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Hugging Face network check failed: {type(exc).__name__}") from None


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
        from PIL import Image
        from projects.generation.providers.huggingface_provider import HuggingFaceProvider

        output = Path("output/huggingface-live-test.png")
        result = HuggingFaceProvider().generate_image(
            "A cinematic realistic landscape photograph at golden hour, natural colors, no text",
            str(output),
        )
        if result.get("status") != "COMPLETED" or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("provider did not produce a non-empty image file")
        with Image.open(output) as image:
            image.verify()
        print("PASS: Hugging Face image generation returned a valid image file.")
        print(f"Image artifact: {output} ({output.stat().st_size} bytes)")
    except Exception as exc:
        # Report the exception class only to avoid accidentally exposing request data.
        print(f"FAIL: image generation failed ({type(exc).__name__}). Check model access, provider availability, and account credits.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
