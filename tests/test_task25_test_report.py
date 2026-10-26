"""Tests for safe, machine-readable Task #25 regression reports."""
import io
import sys
import unittest

from projects.provider_store.task25_test_report import Task25TestResult, build_report


class Task25TestReportTests(unittest.TestCase):
    def test_report_contains_outcomes_counts_and_no_failure_messages(self):
        result = Task25TestResult(io.StringIO(), descriptions=False, verbosity=0)

        class PassingCase(unittest.TestCase):
            def runTest(self):
                self.assertTrue(True)

        class FailingCase(unittest.TestCase):
            def runTest(self):
                self.fail("private-token-must-not-enter-report")

        passed = PassingCase()
        result.startTest(passed)
        result.addSuccess(passed)
        result.stopTest(passed)

        failed = FailingCase()
        result.startTest(failed)
        try:
            failed.runTest()
        except AssertionError:
            result.addFailure(failed, sys.exc_info())
        result.stopTest(failed)

        report = build_report(result, generated_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(report["outcome"], "FAIL")
        self.assertEqual(report["summary"]["tests_run"], 2)
        self.assertEqual(report["summary"]["passed"], 1)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(len(report["tests"]), 2)
        self.assertNotIn("private-token-must-not-enter-report", repr(report))
        self.assertEqual(report["generated_at"], "2026-01-01T00:00:00+00:00")

    def test_report_records_skips_and_pass_outcome(self):
        result = Task25TestResult(io.StringIO(), descriptions=False, verbosity=0)

        class SkippedCase(unittest.TestCase):
            def runTest(self):
                pass

        skipped = SkippedCase()
        result.startTest(skipped)
        result.addSkip(skipped, "environment detail must not be serialized")
        result.stopTest(skipped)

        report = build_report(result, generated_at="fixed")
        self.assertEqual(report["outcome"], "PASS")
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertEqual(report["tests"][0]["status"], "SKIPPED")
        self.assertNotIn("environment detail", repr(report))


if __name__ == "__main__":
    unittest.main()
