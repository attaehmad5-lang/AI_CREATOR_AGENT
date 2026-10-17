import unittest

from projects.provider_store.provider_secret_resolver import ProviderSecretResolver


class ProviderSecretResolverTests(unittest.TestCase):
    def test_environment_reference_resolves_without_accepting_inline_secret(self):
        resolver = ProviderSecretResolver(environ={"HF_TOKEN": "private-token"})
        self.assertEqual(resolver.resolve("env:HF_TOKEN"), "private-token")
        with self.assertRaises(ValueError):
            resolver.resolve("private-token")
        with self.assertRaises(LookupError):
            resolver.resolve("env:MISSING_TOKEN")

    def test_vault_reference_uses_injected_resolver_and_never_echoes_errors(self):
        seen = []
        resolver = ProviderSecretResolver(
            vault_resolver=lambda name: seen.append(name) or "vault-secret",
        )
        self.assertEqual(resolver.resolve("vault:provider/api-key"), "vault-secret")
        self.assertEqual(seen, ["provider/api-key"])
        failing = ProviderSecretResolver(
            vault_resolver=lambda _: (_ for _ in ()).throw(RuntimeError("vault-secret")),
        )
        with self.assertRaises(LookupError) as caught:
            failing.resolve("vault:provider/api-key")
        self.assertNotIn("vault-secret", str(caught.exception))

    def test_invalid_references_and_vault_configuration_fail_closed(self):
        resolver = ProviderSecretResolver()
        with self.assertRaises(LookupError):
            resolver.resolve("vault:provider/api-key")
        for reference in ("env:lowercase", "vault:../secret", "env:HF_TOKEN\n", "env:"):
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError):
                    resolver.resolve(reference)


if __name__ == "__main__":
    unittest.main()
