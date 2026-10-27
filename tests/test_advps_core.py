import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.advanced.advps.accounting import Accounting, estimate_cost
from projects.provider_store.advanced.advps.audit import AuditLog
from projects.provider_store.advanced.advps.catalog import Catalog
from projects.provider_store.advanced.advps.errors import (
    AmbiguousOutcomeError, BudgetExceededError, PaidUsageNotApprovedError,
    PriceUnknownError, SecurityError, ValidationError,
)
from projects.provider_store.advanced.advps.redact import redact, redact_text
from projects.provider_store.advanced.advps.resilience import RetryPolicy, retry_call, CircuitBreaker
from projects.provider_store.advanced.advps.router import Router
from projects.provider_store.advanced.advps.security import check_redirect, enforce_size, validate_endpoint
from projects.provider_store.advanced.advps.storage import AtomicJsonStore, StoreCorruptError


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
