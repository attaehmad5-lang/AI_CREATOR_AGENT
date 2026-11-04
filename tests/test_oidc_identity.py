"""OIDC/JWT identity verification and fail-closed integration tests."""
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from projects.provider_store.oidc_identity import (
    OIDCAuthenticationError,
    OIDCIdentityAuthenticator,
    OIDCJWKSResolver,
)
from projects.provider_store.provider_authorization import ProviderAuthorizationManager
from projects.provider_store.provider_service import ProviderService


class OIDCIdentityAuthenticatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()

    def setUp(self):
        self.now = int(time.time())
        self.authenticator = OIDCIdentityAuthenticator(
            issuer="https://identity.example.test/",
            audience="ai-creator-agent-api",
            key_resolver=lambda token, header: self.public_key,
        )

    def token(self, **overrides):
        claims = {
            "iss": "https://identity.example.test/",
            "aud": "ai-creator-agent-api",
            "sub": "user-123",
            "iat": self.now,
            "exp": self.now + 300,
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "key-1"})

    def test_valid_signed_token_produces_opaque_principal(self):
        principal = self.authenticator.authenticate(self.token())
        self.assertEqual(str(principal), "user-123")
        self.assertEqual(principal.subject, "user-123")
        self.assertEqual(principal.issuer, "https://identity.example.test/")
        self.assertTrue(self.authenticator.is_authenticated_principal(principal))
        self.assertFalse(self.authenticator.is_authenticated_principal("user-123"))

    def test_invalid_signature_is_rejected(self):
        token = jwt.encode(
            {"iss": "https://identity.example.test", "aud": "ai-creator-agent-api",
             "sub": "user-123", "iat": self.now, "exp": self.now + 300},
            self.other_key, algorithm="RS256", headers={"kid": "key-1"},
        )
        with self.assertRaisesRegex(OIDCAuthenticationError, "OIDC_TOKEN_INVALID"):
            self.authenticator.authenticate(token)

    def test_wrong_issuer_and_audience_are_rejected(self):
        for claims in (
            {"iss": "https://attacker.example", "aud": "ai-creator-agent-api"},
            {"iss": "https://identity.example.test", "aud": "another-api"},
        ):
            with self.subTest(claims=claims):
                with self.assertRaises(OIDCAuthenticationError):
                    self.authenticator.authenticate(self.token(**claims))

    def test_expired_and_missing_required_claims_are_rejected(self):
        expired = self.token(exp=self.now - 120)
        with self.assertRaises(OIDCAuthenticationError):
            self.authenticator.authenticate(expired)
        missing = jwt.encode(
            {"iss": "https://identity.example.test", "aud": "ai-creator-agent-api",
             "sub": "user-123", "iat": self.now},
            self.private_key, algorithm="RS256", headers={"kid": "key-1"},
        )
        with self.assertRaises(OIDCAuthenticationError):
            self.authenticator.authenticate(missing)

    def test_algorithm_confusion_and_untrusted_key_headers_are_rejected(self):
        unsigned = jwt.encode(
            {"iss": "https://identity.example.test", "aud": "ai-creator-agent-api",
             "sub": "user-123", "iat": self.now, "exp": self.now + 300},
            key="", algorithm="none",
        )
        with self.assertRaises(OIDCAuthenticationError):
            self.authenticator.authenticate(unsigned)
        evil_header = jwt.encode(
            {"iss": "https://identity.example.test", "aud": "ai-creator-agent-api",
             "sub": "user-123", "iat": self.now, "exp": self.now + 300},
            self.private_key, algorithm="RS256",
            headers={"kid": "key-1", "jku": "https://attacker.invalid/jwks"},
        )
        with self.assertRaises(OIDCAuthenticationError):
            self.authenticator.authenticate(evil_header)

    def test_token_size_and_principal_mapping_fail_closed(self):
        with self.assertRaisesRegex(OIDCAuthenticationError, "OIDC_TOKEN_INVALID"):
            self.authenticator.authenticate("x" * 20_000)
        invalid_mapping = OIDCIdentityAuthenticator(
            issuer="https://identity.example.test",
            audience="ai-creator-agent-api",
            key_resolver=lambda token, header: self.public_key,
            principal_mapper=lambda claims: "bad principal",
        )
        with self.assertRaisesRegex(OIDCAuthenticationError, "OIDC_PRINCIPAL_MAPPING_INVALID"):
            invalid_mapping.authenticate(self.token())

    def test_mfa_is_enforced_using_amr_or_acr(self):
        strict = OIDCIdentityAuthenticator(
            issuer="https://identity.example.test",
            audience="ai-creator-agent-api",
            key_resolver=lambda token, header: self.public_key,
            require_mfa=True,
            required_amr=("mfa", "webauthn"),
            required_acr=("urn:example:loa:2",),
        )
        with self.assertRaisesRegex(OIDCAuthenticationError, "OIDC_MFA_REQUIRED"):
            strict.authenticate(self.token())
        self.assertEqual(str(strict.authenticate(self.token(amr=["pwd", "mfa"]))), "user-123")
        self.assertEqual(str(strict.authenticate(self.token(acr="urn:example:loa:2"))), "user-123")

    def test_future_issued_and_stale_tokens_are_rejected(self):
        strict = OIDCIdentityAuthenticator(
            issuer="https://identity.example.test",
            audience="ai-creator-agent-api",
            key_resolver=lambda token, header: self.public_key,
            max_token_age_seconds=300,
        )
        with self.assertRaises(OIDCAuthenticationError):
            strict.authenticate(self.token(iat=self.now + 600))
        with self.assertRaises(OIDCAuthenticationError):
            strict.authenticate(self.token(iat=self.now - 7200))

    def test_jwks_uri_requires_https_and_exact_allowlisted_host(self):
        for uri, hosts in (
            ("http://identity.example.test/jwks", {"identity.example.test"}),
            ("https://attacker.example/jwks", {"identity.example.test"}),
            ("https://user:pass@identity.example.test/jwks", {"identity.example.test"}),
            ("https://identity.example.test/jwks?next=bad", {"identity.example.test"}),
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError):
                    OIDCJWKSResolver(uri, allowed_hosts=hosts)

    def test_jwks_resolver_rejects_bad_key_usage_and_duplicate_ids(self):
        class Response:
            def __init__(self, body):
                self.body = body
                self.closed = False
            def getcode(self):
                return 200
            def read(self, size):
                return self.body[:size]
            def close(self):
                self.closed = True

        class Opener:
            def __init__(self, document):
                import json
                self.body = json.dumps(document).encode()
            def open(self, request, timeout):
                return Response(self.body)

        resolver = OIDCJWKSResolver(
            "https://identity.example.test/jwks",
            allowed_hosts={"identity.example.test"},
            opener=Opener({"keys": [
                {"kid": "key-1", "kty": "RSA", "use": "enc", "n": "AQAB", "e": "AQAB"}
            ]}),
        )
        with self.assertRaisesRegex(OIDCAuthenticationError, "OIDC_SIGNING_KEY_INVALID"):
            resolver("not-used", {"kid": "key-1", "alg": "RS256"})
        duplicate = OIDCJWKSResolver(
            "https://identity.example.test/jwks",
            allowed_hosts={"identity.example.test"},
            opener=Opener({"keys": [
                {"kid": "same", "kty": "RSA"}, {"kid": "same", "kty": "RSA"}
            ]}),
        )
        with self.assertRaisesRegex(OIDCAuthenticationError, "OIDC_JWKS_INVALID"):
            duplicate("not-used", {"kid": "same", "alg": "RS256"})


if __name__ == "__main__":
    unittest.main()
