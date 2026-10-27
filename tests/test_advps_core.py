import json
import socket
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.advanced.advps.accounting import Accounting, estimate_cost
from projects.provider_store.advanced.advps.audit import AuditLog
from projects.provider_store.advanced.advps.catalog import Catalog
from projects.provider_store.advanced.advps.credentials import CredentialManager
from projects.provider_store.advanced.advps.jobs import JobManager
from projects.provider_store.advanced.advps.cli import main as cli_main
from projects.provider_store.advanced.advps.errors import (
    AmbiguousOutcomeError, BudgetExceededError, PaidUsageNotApprovedError,
    PriceUnknownError, SecurityError, ValidationError,
)
from projects.provider_store.advanced.advps.redact import redact, redact_text
from projects.provider_store.advanced.advps.resilience import RetryPolicy, retry_call, CircuitBreaker
from projects.provider_store.advanced.advps.router import Router
from projects.provider_store.advanced.advps.security import check_redirect, enforce_size, validate_endpoint
from projects.provider_store.advanced.advps.storage import AtomicJsonStore, StoreCorruptError
from projects.provider_store.advanced.advps.transport import SecureHTTPTransport, _PinnedHTTPSConnection
from unittest.mock import patch
import ssl


class _FakeResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self._headers = headers
        self._body = body

    def read(self, limit):
        return self._body[:limit]

    def getheaders(self):
        return list(self._headers.items())


class _FakeConnection:
    def __init__(self, response, calls, host, port, pinned_ip):
        self.response = response
        self.calls = calls
        self.host = host
        self.port = port
        self.pinned_ip = pinned_ip

    def request(self, method, path, body=None, headers=None):
        self.calls.append((self.host, self.port, self.pinned_ip, method, path, body, dict(headers or {})))

    def getresponse(self):
        return self.response

    def close(self):
        pass


class AdvancedProviderStoreCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_store_checksum_backup_and_recovery(self):
        path = self.root / "state.json"
        store = AtomicJsonStore(path)
        store.write({"safe": True})
        backup = store.backup()
        store.write({"safe": False})
        path.write_text('{"data":{"safe":false},"sha256":"wrong"}', encoding="utf-8")
        with self.assertRaises(StoreCorruptError):
            store.read()
        self.assertTrue(store.recover())
        self.assertEqual(store.read(), {"safe": True})
        self.assertTrue(backup.exists())

    def test_catalog_registration_versioning_export_import_and_rollback(self):
        store = AtomicJsonStore(self.root / "catalog.json")
        catalog = Catalog(store, allowlist=["example.com"])
        catalog.register({
            "id": "provider-a", "name": "A", "version": "1.2.3",
            "endpoint": "https://api.example.com/v1", "enabled": True,
            "capabilities": ["text_to_image"], "priority": 2,
            "models": {"m1": {"capabilities": ["text_to_image"], "price_per_unit": 0.02}},
        })
        rev = catalog.revision
        catalog.set_enabled("provider-a", False)
        self.assertEqual(catalog.list(enabled_only=True), [])
        self.assertEqual(catalog.diff_with_revision(rev)["changed"], ["provider-a"])
        catalog.rollback(rev)
        self.assertTrue(catalog.get("provider-a")["enabled"])
        exported = catalog.export_json()
        imported = Catalog(AtomicJsonStore(self.root / "import.json"), allowlist=["example.com"])
        self.assertEqual(imported.import_json(exported), 1)
        self.assertEqual(imported.get("provider-a")["version"], "1.2.3")

    def test_catalog_rejects_private_endpoint_and_bad_version(self):
        with self.assertRaises(SecurityError):
            Catalog(AtomicJsonStore(self.root / "bad.json")).register({
                "id": "private", "version": "1.0.0", "endpoint": "https://127.0.0.1",
            })
        with self.assertRaises(ValidationError):
            Catalog(AtomicJsonStore(self.root / "bad2.json")).register({
                "id": "bad", "version": "latest",
            })

    def test_catalog_rejects_secret_values_and_nonfinite_prices(self):
        catalog = Catalog(AtomicJsonStore(self.root / "secret-catalog.json"))
        with self.assertRaises(ValidationError):
            catalog.register({
                "id": "secret-provider", "metadata": {"api_key": "plain-secret"},
            })
        with self.assertRaises(ValidationError):
            catalog.register({
                "id": "nan-price", "models": {"m": {"price_per_unit": float("nan")}},
            })
        safe = catalog.register({
            "id": "env-provider", "metadata": {"api_key_env": "PROVIDER_TOKEN"},
        })
        self.assertEqual(safe["metadata"]["api_key_env"], "PROVIDER_TOKEN")

    def test_security_redirect_and_size_guards(self):
        with self.assertRaises(SecurityError):
            validate_endpoint("http://example.com")
        with self.assertRaises(SecurityError):
            validate_endpoint("https://localhost")
        with self.assertRaises(SecurityError):
            check_redirect("https://example.com", "http://example.com")
        self.assertEqual(enforce_size(b"abc", 3), b"abc")
        with self.assertRaises(SecurityError):
            enforce_size(b"abcd", 3)

    def test_secret_redaction_does_not_mutate_original(self):
        original = {"api_key": "secret-value", "message": "Bearer abcdefghijklmnop"}
        safe = redact(original)
        self.assertEqual(original["api_key"], "secret-value")
        self.assertNotIn("secret-value", json.dumps(safe))
        self.assertNotIn("abcdefghijklmnop", redact_text(original["message"]))

    def test_audit_log_hash_chain_and_secret_redaction(self):
        log = AuditLog(self.root / "audit.jsonl")
        log.append("provider.test", api_token="secret-value", outcome="ok")
        self.assertEqual(log.verify(), 1)
        self.assertNotIn("secret-value", (self.root / "audit.jsonl").read_text(encoding="utf-8"))

    def test_accounting_requires_paid_approval_and_enforces_budget(self):
        accounting = Accounting(AtomicJsonStore(self.root / "usage.json"))
        accounting.set_budget(1.0)
        with self.assertRaises(PaidUsageNotApprovedError):
            accounting.reserve_and_record("p", "m", 1, 0.6, key="k1")
        accounting.approve_paid("p", 2.0)
        accounting.reserve_and_record("p", "m", 1, 0.6, key="k1")
        with self.assertRaises(BudgetExceededError):
            accounting.reserve_and_record("p", "m", 1, 0.5, key="k2")
        self.assertEqual(accounting.total_spend("p"), 0.6)

    def test_accounting_rejects_nonfinite_and_noninteger_limits(self):
        accounting = Accounting(AtomicJsonStore(self.root / "invalid-limits.json"))
        with self.assertRaises(ValidationError):
            accounting.set_budget(float("nan"))
        with self.assertRaises(ValidationError):
            accounting.set_quota("p", 1.5)
        with self.assertRaises(ValidationError):
            accounting.approve_paid("p", float("inf"))
        with self.assertRaises(ValidationError):
            accounting.reserve_and_record("p", "m", float("nan"), 0.0)

    def test_unknown_price_is_never_guessed(self):
        with self.assertRaises(PriceUnknownError):
            estimate_cost({"price_per_unit": None}, 1)
        self.assertEqual(estimate_cost({"price_per_unit": 0.2}, 3), 0.6)

    def test_ambiguous_provider_outcome_is_not_retried_by_default(self):
        calls = []
        def fail():
            calls.append(1)
            raise AmbiguousOutcomeError()
        with self.assertRaises(AmbiguousOutcomeError):
            retry_call(fail, RetryPolicy(max_attempts=3), sleep=lambda _: None)
        self.assertEqual(len(calls), 1)

    def test_circuit_breaker_opens_and_resets(self):
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout=5, clock=lambda: now[0])
        breaker.record_failure()
        self.assertEqual(breaker.state, CircuitBreaker.OPEN)
        now[0] = 6.0
        self.assertEqual(breaker.state, CircuitBreaker.HALF_OPEN)
        breaker.allow()
        breaker.record_success()
        self.assertEqual(breaker.state, CircuitBreaker.CLOSED)

    def test_cli_add_list_simulate_backup_verify_and_diagnose(self):
        import contextlib
        import io
        home = self.root / "cli-home"
        provider_file = self.root / "provider.json"
        provider_file.write_text(json.dumps({
            "id": "cli-provider", "version": "1.0.0", "enabled": True,
            "capabilities": ["text_to_image"], "priority": 1,
            "models": {"m": {"capabilities": ["text_to_image"], "price_per_unit": 0.01}},
        }), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main(["--home", str(home), "add", str(provider_file)]), 0)
            self.assertEqual(cli_main(["--home", str(home), "list", "--enabled-only"]), 0)
            self.assertEqual(cli_main(["--home", str(home), "simulate", "text_to_image"]), 0)
            self.assertEqual(cli_main(["--home", str(home), "backup"]), 0)
            self.assertEqual(cli_main(["--home", str(home), "verify"]), 0)
            self.assertEqual(cli_main(["--home", str(home), "diagnose"]), 0)
        self.assertIn("cli-provider", output.getvalue())
        self.assertIn('"cli-provider"', output.getvalue())
        self.assertIn("audit entries verified", output.getvalue())

    def test_router_never_fails_over_after_ambiguous_paid_outcome(self):
        catalog = Catalog(AtomicJsonStore(self.root / "ambiguous-route.json"))
        for pid, priority in (("provider-a", 1), ("provider-b", 2)):
            catalog.register({
                "id": pid, "version": "1.0.0", "enabled": True,
                "capabilities": ["text_to_image"], "priority": priority,
                "models": {"m": {"capabilities": ["text_to_image"], "price_per_unit": 0.01}},
            })
        calls = []
        def ambiguous(provider, model):
            calls.append(provider["id"])
            raise AmbiguousOutcomeError("provider may have charged")
        router = Router(catalog, sleep=lambda _: None)
        with self.assertRaises(Exception) as caught:
            router.execute("text_to_image", ambiguous, idempotent=False)
        self.assertIn("ambiguous outcome", str(caught.exception))
        self.assertEqual(calls, ["provider-a"])

    def test_credential_resolution_missing_expired_and_in_memory_refresh(self):
        now = [100.0]
        env = {"PROVIDER_TOKEN": "old-token", "PROVIDER_TOKEN_EXPIRY": "110"}
        manager = CredentialManager(environ=env, clock=lambda: now[0], refresh_before_seconds=60)
        with self.assertRaises(Exception) as missing:
            manager.resolve("missing-provider", "MISSING_TOKEN")
        self.assertIn("credential_unavailable", getattr(missing.exception, "code", ""))
        with self.assertRaises(Exception) as expired:
            manager.resolve("provider-a", "PROVIDER_TOKEN", expires_at_env="PROVIDER_TOKEN_EXPIRY")
        self.assertIn("credential_expired", getattr(expired.exception, "code", ""))

        calls = []
        manager.register_refresher(
            "provider-a",
            lambda provider, token: calls.append((provider, token)) or {"token": "new-token", "expires_at": 500.0},
        )
        self.assertEqual(manager.resolve("provider-a", "PROVIDER_TOKEN", expires_at_env="PROVIDER_TOKEN_EXPIRY"), "new-token")
        self.assertEqual(manager.resolve("provider-a", "PROVIDER_TOKEN", expires_at_env="PROVIDER_TOKEN_EXPIRY"), "new-token")
        self.assertEqual(calls, [("provider-a", "old-token")])
        self.assertEqual(env["PROVIDER_TOKEN"], "old-token")
        manager.clear_cache("provider-a")
        with self.assertRaises(Exception):
            manager.resolve("provider-a", "MISSING_TOKEN")

    def test_credential_expiry_validation_and_bad_refresh_response(self):
        manager = CredentialManager(environ={"TOKEN": "old", "EXPIRY": "not-a-number"}, clock=lambda: 100.0)
        with self.assertRaises(ValidationError):
            manager.resolve("provider-a", "TOKEN", expires_at_env="EXPIRY")
        manager = CredentialManager(environ={"TOKEN": "old", "EXPIRY": "110"}, clock=lambda: 100.0, refresh_before_seconds=60)
        manager.register_refresher("provider-a", lambda provider, token: {"token": "new", "expires_at": float("nan")})
        with self.assertRaises(Exception) as error:
            manager.resolve("provider-a", "TOKEN", expires_at_env="EXPIRY")
        self.assertIn("credential_expired", getattr(error.exception, "code", ""))

    def test_job_idempotency_leases_completion_and_secret_safe_storage(self):
        now = [100.0]
        path = self.root / "jobs.json"
        manager = JobManager(AtomicJsonStore(path), clock=lambda: now[0])
        job = manager.create("provider-a", "text_to_image", {"prompt": "sunrise"}, idempotency_key="private-idem-key")
        duplicate = manager.create("provider-a", "text_to_image", {"prompt": "sunrise"}, idempotency_key="private-idem-key")
        self.assertEqual(duplicate["id"], job["id"])
        self.assertTrue(duplicate["duplicate"])
        self.assertNotIn("private-idem-key", path.read_text(encoding="utf-8"))
        with self.assertRaises(ValidationError):
            manager.create("provider-a", "text_to_image", {"prompt": "different"}, idempotency_key="private-idem-key")
        with self.assertRaises(ValidationError):
            manager.create("provider-a", "video_generation", {"prompt": "sunrise"}, idempotency_key="private-idem-key")
        with self.assertRaises(ValidationError):
            manager.create("provider-a", "text_to_image", {"prompt": "x"}, metadata={"nested": {"api_key": "secret"}})
        with self.assertRaises(ValidationError):
            manager.create("provider-a", "text_to_image", {"prompt": "x"}, metadata={"note": "Bearer abcdefghijklmnop"})
        claimed = manager.claim(job["id"], "worker-1", lease_seconds=10)
        with self.assertRaises(ValidationError):
            manager.claim(job["id"], "worker-2", lease_seconds=10)
        completed = manager.complete(job["id"], "worker-1", "asset://result/123")
        self.assertEqual(completed["status"], "SUCCEEDED")
        self.assertEqual(manager.list("SUCCEEDED")[0]["id"], job["id"])
        secret_result = manager.create("provider-a", "text_to_image", {"prompt": "secret-url-test"})
        manager.claim(secret_result["id"], "worker-2", lease_seconds=10)
        with self.assertRaises(ValidationError):
            manager.complete(secret_result["id"], "worker-2", "https://store.example/result?token=supersecretvalue")

    def test_job_crash_recovery_retry_exhaustion_and_cancellation(self):
        now = [0.0]
        manager = JobManager(AtomicJsonStore(self.root / "jobs-recovery.json"), clock=lambda: now[0])
        job = manager.create("p", "video_generation", {"prompt": "x"}, max_attempts=2)
        manager.claim(job["id"], "worker-a", lease_seconds=5)
        now[0] = 6.0
        self.assertEqual(manager.recover_stale(), {"requeued": 1, "failed": 0})
        manager.claim(job["id"], "worker-b", lease_seconds=5)
        now[0] = 12.0
        self.assertEqual(manager.recover_stale(), {"requeued": 0, "failed": 1})
        self.assertEqual(manager.get(job["id"])["status"], "FAILED")
        other = manager.create("p", "text_to_image", {"prompt": "cancel"})
        self.assertEqual(manager.cancel(other["id"])["status"], "CANCELLED")
        with self.assertRaises(ValidationError):
            manager.fail(other["id"], "worker", "error message with secret")

    def test_secure_transport_pins_validated_ip_and_returns_bounded_response(self):
        calls = []
        def resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        def factory(host, port, pinned_ip, timeout, context):
            return _FakeConnection(_FakeResponse(200, {"Content-Type": "text/plain"}, b"ok"), calls, host, port, pinned_ip)
        transport = SecureHTTPTransport(resolver=resolver, connection_factory=factory)
        result = transport.request("GET", "https://example.com/path?q=1")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], b"ok")
        self.assertEqual(result["redirects"], 0)
        self.assertEqual(calls[0][2], "93.184.216.34")
        self.assertEqual(calls[0][4], "/path?q=1")
        self.assertEqual(calls[0][5], None)

    def test_secure_transport_revalidates_redirect_and_pins_each_hop(self):
        calls = []
        responses = [
            _FakeResponse(302, {"Location": "/next"}, b""),
            _FakeResponse(200, {}, b"done"),
        ]
        def resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        def factory(host, port, pinned_ip, timeout, context):
            return _FakeConnection(responses.pop(0), calls, host, port, pinned_ip)
        transport = SecureHTTPTransport(resolver=resolver, connection_factory=factory)
        result = transport.request("GET", "https://example.com/start")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["url"], "https://example.com/next")
        self.assertEqual(result["redirects"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], calls[1][2])

    def test_secure_transport_rejects_private_dns_and_oversized_payloads(self):
        private_resolver = lambda host, port, type=None: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))
        ]
        transport = SecureHTTPTransport(resolver=private_resolver, connection_factory=lambda *a: None)
        with self.assertRaises(SecurityError):
            transport.request("GET", "https://example.com")
        public_resolver = lambda host, port, type=None: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ]
        bounded = SecureHTTPTransport(
            resolver=public_resolver, max_request_bytes=2, max_response_bytes=2,
            connection_factory=lambda *a: None,
        )
        with self.assertRaises(SecurityError):
            bounded.request("POST", "https://example.com", body=b"long")
        def large_factory(host, port, pinned_ip, timeout, context):
            return _FakeConnection(_FakeResponse(200, {}, b"long"), [], host, port, pinned_ip)
        bounded.connection_factory = large_factory
        with self.assertRaises(SecurityError):
            bounded.request("GET", "https://example.com")

    def test_secure_transport_rejects_unsafe_headers_and_redirect_downgrade(self):
        calls = []
        def resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        transport = SecureHTTPTransport(
            resolver=resolver,
            connection_factory=lambda host, port, ip, timeout, context: _FakeConnection(
                _FakeResponse(302, {"Location": "http://example.com/insecure"}, b""), calls, host, port, ip
            ),
        )
        with self.assertRaises(SecurityError):
            transport.request("GET", "https://example.com")
        with self.assertRaises(SecurityError):
            transport.request("GET", "https://example.com", headers={"Host": "attacker.example"})

    def test_secure_transport_strips_credentials_on_cross_origin_redirect(self):
        calls = []
        responses = [
            _FakeResponse(302, {"Location": "https://other.example/final"}, b""),
            _FakeResponse(200, {}, b"ok"),
        ]
        def resolver(host, port, type=None):
            ip = "93.184.216.34" if host == "example.com" else "1.1.1.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        def factory(host, port, pinned_ip, timeout, context):
            return _FakeConnection(responses.pop(0), calls, host, port, pinned_ip)
        transport = SecureHTTPTransport(resolver=resolver, connection_factory=factory)
        result = transport.request(
            "GET", "https://example.com/start",
            headers={"Authorization": "Bearer secret-token", "X-API-Key": "secret-key", "Accept": "application/json"},
        )
        self.assertEqual(result["status"], 200)
        self.assertIn("Authorization", calls[0][6])
        self.assertIn("X-API-Key", calls[0][6])
        self.assertNotIn("Authorization", calls[1][6])
        self.assertNotIn("X-API-Key", calls[1][6])
        self.assertEqual(calls[1][6]["Accept"], "application/json")

    def test_secure_transport_redirect_limit_and_post_to_get_semantics(self):
        calls = []
        responses = [
            _FakeResponse(303, {"Location": "/result"}, b""),
            _FakeResponse(200, {}, b"ok"),
        ]
        resolver = lambda host, port, type=None: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ]
        def factory(host, port, pinned_ip, timeout, context):
            return _FakeConnection(responses.pop(0), calls, host, port, pinned_ip)
        transport = SecureHTTPTransport(resolver=resolver, connection_factory=factory)
        result = transport.request("POST", "https://example.com/start", body=b"payload")
        self.assertEqual(result["body"], b"ok")
        self.assertEqual(calls[0][3], "POST")
        self.assertEqual(calls[1][3], "GET")
        self.assertIsNone(calls[1][5])

        loop_responses = [_FakeResponse(302, {"Location": "/again"}, b"") for _ in range(3)]
        loop = SecureHTTPTransport(
            resolver=resolver, max_redirects=1,
            connection_factory=lambda host, port, ip, timeout, context:
                _FakeConnection(loop_responses.pop(0), [], host, port, ip),
        )
        with self.assertRaises(SecurityError):
            loop.request("GET", "https://example.com/start")

    def test_secure_transport_rejects_invalid_configuration_inputs_and_headers(self):
        with self.assertRaises(ValueError):
            SecureHTTPTransport(timeout=0)
        with self.assertRaises(ValueError):
            SecureHTTPTransport(max_redirects=11)
        resolver = lambda host, port, type=None: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ]
        transport = SecureHTTPTransport(resolver=resolver, connection_factory=lambda *a: None)
        with self.assertRaises(SecurityError):
            transport.request("TRACE", "https://example.com")
        with self.assertRaises(SecurityError):
            transport.request("GET", "https://example.com", body="not bytes")
        with self.assertRaises(SecurityError):
            transport.request("GET", "https://example.com", headers={"X-Test": "bad\r\nvalue"})
        with self.assertRaises(SecurityError):
            transport.request("GET", "https://example.com", headers=["bad"])
        allowlisted = SecureHTTPTransport(allowlist=["allowed.example"], resolver=resolver, connection_factory=lambda *a: None)
        with self.assertRaises(SecurityError):
            allowlisted.request("GET", "https://other.example")

    def test_pinned_https_connection_uses_ip_for_socket_and_hostname_for_tls(self):
        class FakeSocket:
            def close(self):
                pass
        class FakeContext:
            def __init__(self):
                self.hostname = None
            def wrap_socket(self, raw, server_hostname):
                self.hostname = server_hostname
                return FakeSocket()
        context = FakeContext()
        connection = _PinnedHTTPSConnection("api.example.com", 443, "93.184.216.34", 1, ssl.create_default_context())
        connection._context = context
        with patch("projects.provider_store.advanced.advps.transport.socket.create_connection", return_value=FakeSocket()) as create:
            connection.connect()
        create.assert_called_once_with(("93.184.216.34", 443), 1, None)
        self.assertEqual(context.hostname, "api.example.com")
        connection.close()

    def test_router_simulates_and_executes_with_provider_callback(self):
        catalog = Catalog(AtomicJsonStore(self.root / "route.json"))
        catalog.register({
            "id": "provider-a", "version": "1.0.0", "enabled": True,
            "capabilities": ["text_to_image"], "priority": 1,
            "models": {"m1": {"capabilities": ["text_to_image"], "price_per_unit": 0.01}},
        })
        router = Router(catalog, sleep=lambda _: None)
        self.assertEqual(router.simulate("text_to_image"), ["provider-a"])
        selected, result = router.execute("text_to_image", lambda provider, model: "ok")
        self.assertEqual((selected, result), ("provider-a", "ok"))


if __name__ == "__main__":
    unittest.main()
