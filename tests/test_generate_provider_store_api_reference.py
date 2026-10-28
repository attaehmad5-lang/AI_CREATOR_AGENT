import tempfile
import unittest
from pathlib import Path

from projects.provider_store.generate_api_reference import main, render_api_reference


class GenerateApiReferenceTests(unittest.TestCase):
    def test_render_is_deterministic_and_documents_public_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "provider_store"
            root.mkdir()
            (root / "sample.py").write_text(
                '"""Sample module docs."""\n'
                'def public_call(value: int) -> str:\n'
                '    """Return a sample value."""\n'
                '    return str(value)\n'
                'def _private_call():\n'
                '    pass\n'
                'async def async_public(value):\n'
                '    return value\n'
                'class Undocumented:\n'
                '    pass\n'
                'class PublicManager:\n'
                '    """Manage a sample."""\n'
                '    def execute(self, enabled: bool=True) -> None:\n'
                '        """Execute sample operation."""\n'
                '    def _hidden(self):\n'
                '        pass\n',
                encoding="utf-8",
            )
            first = render_api_reference(root)
            second = render_api_reference(root)
            self.assertEqual(first, second)
            self.assertIn("public_call(value: int) -> str", first)
            self.assertIn("async def async_public(value)", first)
            self.assertIn("class Undocumented", first)
            self.assertIn("class PublicManager", first)
            self.assertIn("execute(self, enabled: bool = True) -> None", first)
            self.assertNotIn("_private_call", first)
            self.assertNotIn("_hidden", first)

    def test_check_mode_detects_stale_reference_and_passes_current_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "provider_store"
            root.mkdir()
            (root / "example.py").write_text("def public_api():\n    pass\n", encoding="utf-8")
            output = root / "API.md"
            self.assertEqual(main(["--package-root", str(root), "--output", str(output)]), 0)
            self.assertEqual(main(["--package-root", str(root), "--output", str(output), "--check"]), 0)
            output.write_text("stale", encoding="utf-8")
            self.assertEqual(main(["--package-root", str(root), "--output", str(output), "--check"]), 1)

    def test_invalid_python_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "provider_store"
            root.mkdir()
            (root / "broken.py").write_text("def broken(:\\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot parse"):
                render_api_reference(root)

    def test_main_returns_error_for_missing_package_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            self.assertEqual(main(["--package-root", str(missing)]), 2)

    def test_missing_package_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing"
            with self.assertRaises(ValueError):
                render_api_reference(missing)


if __name__ == "__main__":
    unittest.main()
