import random
import string
import unittest

from projects.provider_store.connection_manager import ConnectionManager
from projects.provider_store.provider_trace_manager import parse_traceparent


class ProviderStoreFuzzRegressionTests(unittest.TestCase):
    def test_catalog_schema_validation_is_total_for_seeded_json_like_inputs(self):
        rng = random.Random(250114)
        scalar_values = [None, True, False, 0, -1, 3.14, "", "x", [], {}]
        for index in range(500):
            provider_count = rng.randrange(0, 8)
            providers = []
            for _ in range(provider_count):
                provider = {}
                for key in ("id", "name", "category", "auth_type", "connection_type", "enabled", "allowed_hosts"):
                    if rng.random() < 0.55:
                        if key == "id":
                            provider[key] = "".join(
                                rng.choice(string.printable) for _ in range(rng.randrange(0, 110))
                            )
                        else:
                            provider[key] = rng.choice(scalar_values)
                providers.append(provider if rng.random() < 0.85 else rng.choice(scalar_values))
            candidate = {"version": rng.choice(["3.0", "x", True, None]), "providers": providers}
            result = ConnectionManager.validate_catalog_schema(candidate)
            self.assertIsInstance(result, dict, msg=f"case={index}")
            self.assertIn("valid", result, msg=f"case={index}")
            self.assertIsInstance(result["valid"], bool, msg=f"case={index}")
            if result["valid"]:
                self.assertTrue(all(isinstance(item, dict) for item in candidate["providers"]))

    def test_traceparent_parser_handles_seeded_arbitrary_text_without_exceptions(self):
        rng = random.Random(250115)
        alphabet = string.ascii_letters + string.digits + "-_:. /\t\n"
        for index in range(1000):
            candidate = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 100)))
            result = parse_traceparent(candidate)
            self.assertIn(result["status"], {"VALID", "INVALID_TRACEPARENT"}, msg=f"case={index}")
            if result["status"] == "VALID":
                self.assertEqual(len(result["trace_id"]), 32)
                self.assertEqual(len(result["parent_span_id"]), 16)


if __name__ == "__main__":
    unittest.main()
