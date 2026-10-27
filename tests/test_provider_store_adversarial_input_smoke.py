import json
import random
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.output_validator import ProviderOutputValidator


class ProviderStoreAdversarialInputSmokeTests(unittest.TestCase):
    def test_catalog_schema_validator_handles_deterministic_malformed_json_shapes(self):
        rng = random.Random(20261027)
        atoms = [
            None, True, False, 0, -1, 1.5, float("nan"), float("inf"),
            "", " ", "\x00", "https://127.0.0.1/", "Bearer secret",
            [], {}, {"api_key": "never-persist"}, ["nested", {"token": "redacted"}],
        ]

        for index in range(300):
            providers = []
            for _ in range(rng.randint(0, 4)):
                provider = {}
                for key in rng.sample(
                    ["id", "name", "category", "auth_type", "connection_type",
                     "enabled", "allowed_hosts", "official_api_url", "token_env",
                     "capabilities", "subscription_required", "priority"],
                    rng.randint(0, 10),
                ):
                    value = rng.choice(atoms)
                    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
                        value = str(value)
                    provider[key] = value
                providers.append(provider)
            candidate = {
                "version": rng.choice(["1.0", "3.0", None, 3, "bad"]),
                "providers": providers,
            }
            result = ConnectionManager.validate_catalog_schema(candidate)
            self.assertIsInstance(result, dict, f"case {index}")
            self.assertIsInstance(result.get("valid"), bool, f"case {index}")
            self.assertIsInstance(result.get("errors"), list, f"case {index}")

    def test_output_validator_never_crashes_on_random_untrusted_artifact_bytes(self):
        rng = random.Random(731)
        validator = ProviderOutputValidator(max_size_bytes=4096)
        with tempfile.TemporaryDirectory() as temp_dir:
            for index in range(120):
                path = Path(temp_dir) / f"sample-{index}.png"
                path.write_bytes(bytes(rng.randrange(0, 256) for _ in range(rng.randrange(0, 256))))
                result = validator.validate_file(path, modality="image")
                self.assertIsInstance(result, dict, f"case {index}")
                self.assertIsInstance(result.get("valid"), bool, f"case {index}")


if __name__ == "__main__":
    unittest.main()
