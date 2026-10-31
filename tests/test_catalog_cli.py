import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from projects.provider_store.catalog_cli import main
from projects.provider_store.provider_audit_log import ProviderAuditLog


REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / "projects" / "provider_store" / "provider_catalog.json"


class CatalogCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.current = self.root / "current.json"
        self.current.write_bytes(CATALOG.read_bytes())
        self.candidate = self.root / "candidate.json"
        self.catalog = json.loads(self.current.read_text(encoding="utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main([str(arg) for arg in args])
        return code, json.loads(output.getvalue())

    def test_apply_is_dry_run_by_default(self):
        candidate = dict(self.catalog)
        candidate["version"] = "9.9"
        self.candidate.write_text(json.dumps(candidate), encoding="utf-8")
        original = self.current.read_bytes()
        code, result = self.invoke("apply", self.current, self.candidate)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "CATALOG_DIFF_DRY_RUN")
        self.assertEqual(self.current.read_bytes(), original)

    def test_removal_needs_explicit_approval(self):
        candidate = json.loads(json.dumps(self.catalog))
        candidate["providers"].pop()
        self.candidate.write_text(json.dumps(candidate), encoding="utf-8")
        original = self.current.read_bytes()
        code, result = self.invoke("apply", self.current, self.candidate, "--apply")
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "CATALOG_DIFF_REMOVAL_APPROVAL_REQUIRED")
        self.assertEqual(self.current.read_bytes(), original)

    def test_snapshot_history_and_restore_use_safe_output(self):
        history = self.root / "history"
        code, created = self.invoke("snapshot", self.current, history)
        self.assertEqual(code, 0)
        snapshot = history / created["snapshot"]
        self.assertTrue(snapshot.is_file())
        code, listed = self.invoke("history", history)
        self.assertEqual(code, 0)
        self.assertEqual(listed["snapshots"][0]["name"], snapshot.name)
        self.assertNotIn("providers", json.dumps(listed))
        altered = json.loads(self.current.read_text(encoding="utf-8"))
        altered["version"] = "9.9"
        self.current.write_text(json.dumps(altered), encoding="utf-8")
        code, restored = self.invoke("restore", snapshot, self.current)
        self.assertEqual(code, 0)
        self.assertTrue(restored["restored"])
        self.assertTrue(Path(str(self.current) + ".bak").is_file())
        self.assertEqual(json.loads(self.current.read_text(encoding="utf-8"))["version"], self.catalog["version"])

    def test_prune_command_is_preview_only_until_apply_is_explicit(self):
        history = self.root / "history"
        history.mkdir()
        for day in range(1, 4):
            (history / f"catalog-2026010{day}T000000000000Z.json").write_bytes(
                self.current.read_bytes()
            )
        original = sorted(path.name for path in history.glob("catalog-*.json"))

        code, preview = self.invoke("prune", history, "--keep", "1")
        self.assertEqual(code, 0)
        self.assertFalse(preview["applied"])
        self.assertEqual(len(preview["would_delete"]), 2)
        self.assertEqual(sorted(path.name for path in history.glob("catalog-*.json")), original)

        code, applied = self.invoke("prune", history, "--keep", "1", "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(applied["applied"])
        self.assertEqual(len(applied["deleted"]), 2)
        self.assertEqual(len(list(history.glob("catalog-*.json"))), 1)

    def test_audit_option_records_catalog_operation_with_hash_chain(self):
        destination = self.root / "audited-export.json"
        audit_path = self.root / "catalog-audit.jsonl"
        code, result = self.invoke(
            "export", self.current, destination, "--audit-log", audit_path
        )
        self.assertEqual(code, 0)
        self.assertTrue(result["exported"])
        self.assertTrue(result["audit"]["saved"])
        chain = ProviderAuditLog(audit_path).verify_chain()
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["count"], 2)
        events = [
            json.loads(line)["event"]
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events, ["catalog.export.started", "catalog.export.completed"])

    def test_audit_write_failure_blocks_catalog_mutation(self):
        destination = self.root / "must-not-exist.json"
        target = self.root / "audit-target.jsonl"
        target.write_text("", encoding="utf-8")
        audit_link = self.root / "audit-link.jsonl"
        try:
            audit_link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this platform")
        code, result = self.invoke(
            "export", self.current, destination, "--audit-log", audit_link
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "AUDIT_LOG_REQUIRED")
        self.assertFalse(destination.exists())

    def test_audited_operation_failure_records_terminal_event(self):
        missing_source = self.root / "missing-catalog.json"
        destination = self.root / "failed-export.json"
        audit_path = self.root / "failed-audit.jsonl"

        code, result = self.invoke(
            "export", missing_source, destination, "--audit-log", audit_path
        )

        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CATALOG_OPERATION_FAILED")
        self.assertEqual(result["operation"], "export")
        self.assertTrue(result["audit"]["saved"])
        self.assertFalse(destination.exists())
        chain = ProviderAuditLog(audit_path).verify_chain()
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["count"], 2)
        events = [
            json.loads(line)["event"]
            for line in audit_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events, ["catalog.export.started", "catalog.export.completed"])

    def test_unexpected_operation_exception_is_audited_without_traceback(self):
        from unittest.mock import patch

        destination = self.root / "unexpected-failure.json"
        audit_path = self.root / "unexpected-audit.jsonl"
        with patch(
            "projects.provider_store.catalog_cli.export_catalog",
            side_effect=RuntimeError("internal detail must not be emitted"),
        ):
            code, result = self.invoke(
                "export", self.current, destination, "--audit-log", audit_path
            )

        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "CATALOG_OPERATION_FAILED")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn("internal detail", json.dumps(result))
        self.assertTrue(result["audit"]["saved"])
        self.assertEqual(ProviderAuditLog(audit_path).verify_chain()["count"], 2)
        self.assertFalse(destination.exists())

    def test_export_command_creates_valid_catalog(self):
        destination = self.root / "exported" / "catalog.json"
        code, result = self.invoke("export", self.current, destination)
        self.assertEqual(code, 0)
        self.assertTrue(result["exported"])
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), self.catalog)


    def test_successful_mutation_returns_audit_failure_exit_when_completion_cannot_be_saved(self):
        from unittest.mock import patch

        destination = self.root / "audit-completion-failure.json"
        audit_path = self.root / "audit-completion-failure.jsonl"
        with patch.object(
            ProviderAuditLog,
            "record",
            side_effect=[
                {"status": "AUDIT_EVENT_RECORDED", "saved": True},
                OSError("audit storage unavailable"),
            ],
        ):
            code, result = self.invoke(
                "export", self.current, destination, "--audit-log", audit_path
            )

        # The export itself is atomic and succeeds, but the CLI must not report
        # overall success when its required terminal audit event was not saved.
        self.assertTrue(destination.is_file())
        self.assertEqual(code, 4)
        self.assertTrue(result["exported"])
        self.assertEqual(result["audit"]["status"], "AUDIT_LOG_WRITE_FAILED")
        self.assertFalse(result["audit"]["saved"])


if __name__ == "__main__":
    unittest.main()
