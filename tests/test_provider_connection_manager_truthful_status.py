import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "projects" / "generation" / "provider_connection_manager.py"
SPEC = importlib.util.spec_from_file_location("provider_connection_manager", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_credentials_do_not_claim_live_connection():
    with patch.dict(os.environ, {
        "AI_IMAGE_API_KEY": "image-secret-placeholder",
        "AI_VIDEO_API_KEY": "video-secret-placeholder",
        "AI_VOICE_API_KEY": "voice-secret-placeholder",
    }, clear=True):
        result = MODULE.build_result()
    assert result["api_keys_detected"] == 3
    assert result["providers_connected"] == 0
    assert result["real_provider_call"] is False
    assert all(item["connected"] is False for item in result["connections"])
    assert result["status"] == "DEGRADED"


def test_missing_credentials_block_connection_readiness():
    with patch.dict(os.environ, {}, clear=True):
        result = MODULE.build_result()
    assert result["api_keys_detected"] == 0
    assert result["providers_connected"] == 0
    assert result["status"] == "BLOCKED"
    assert all(item["connection_state"] == "WAITING_FOR_API_KEY" for item in result["connections"])


def test_whitespace_only_credentials_are_not_detected():
    with patch.dict(os.environ, {
        "AI_IMAGE_API_KEY": "   ",
        "AI_VIDEO_API_KEY": "",
        "AI_VOICE_API_KEY": "\\t",
    }, clear=True):
        result = MODULE.build_result()
    assert result["api_keys_detected"] == 0
    assert result["status"] == "BLOCKED"
