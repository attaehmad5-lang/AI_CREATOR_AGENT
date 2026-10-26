"""Run Task #25's cross-platform suite and emit a safe machine-readable report.

The report contains test identifiers, outcomes, durations, environment metadata and
aggregate counts only. Failure messages and tracebacks remain in CI logs, not the
downloadable artifact, to avoid accidentally publishing secrets from exception text.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Task25TestResult(unittest.TextTestResult):
    """Record per-test outcomes without serializing exception messages."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.records: dict[str, dict[str, Any]] = {}
        self._started_at: dict[str, float] = {}

    def startTest(self, test: unittest.case.TestCase) -> None:
        test_id = test.id()
        self._started_at[test_id] = time.perf_counter()
        self.records[test_id] = {"test_id": test_id, "status": "RUNNING"}
        super().startTest(test)

    def stopTest(self, test: unittest.case.TestCase) -> None:
        test_id = test.id()
        record = self.records.setdefault(test_id, {"test_id": test_id})
        if record.get("status") == "RUNNING":
            record["status"] = "PASSED"
        started = self._started_at.pop(test_id, None)
        if started is not None:
            record["duration_ms"] = round(max(0.0, time.perf_counter() - started) * 1000, 3)
        super().stopTest(test)

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        self.records.setdefault(test.id(), {"test_id": test.id()})["status"] = "PASSED"
        super().addSuccess(test)

    def addFailure(self, test: unittest.case.TestCase, err: Any) -> None:
        self.records.setdefault(test.id(), {"test_id": test.id()})["status"] = "FAILED"
        super().addFailure(test, err)

    def addError(self, test: unittest.case.TestCase, err: Any) -> None:
        self.records.setdefault(test.id(), {"test_id": test.id()})["status"] = "ERROR"
        super().addError(test, err)

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        self.records.setdefault(test.id(), {"test_id": test.id()})["status"] = "SKIPPED"
        super().addSkip(test, reason)

    def addExpectedFailure(self, test: unittest.case.TestCase, err: Any) -> None:
        self.records.setdefault(test.id(), {"test_id": test.id()})["status"] = "EXPECTED_FAILURE"
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        self.records.setdefault(test.id(), {"test_id": test.id()})["status"] = "UNEXPECTED_SUCCESS"
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test: unittest.case.TestCase, subtest: Any, err: Any) -> None:
        if err is not None:
            self.records.setdefault(test.id(), {"test_id": test.id()})["status"] = "FAILED"
        super().addSubTest(test, subtest, err)


def build_report(
    result: Task25TestResult,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic-shape report while excluding failure messages."""
    records = sorted(result.records.values(), key=lambda item: item["test_id"])
    counts = {
        "passed": sum(item["status"] == "PASSED" for item in records),
        "failed": sum(item["status"] == "FAILED" for item in records),
        "errors": sum(item["status"] == "ERROR" for item in records),
        "skipped": sum(item["status"] == "SKIPPED" for item in records),
        "expected_failures": sum(item["status"] == "EXPECTED_FAILURE" for item in records),
        "unexpected_successes": sum(item["status"] == "UNEXPECTED_SUCCESS" for item in records),
    }
    return {
        "schema_version": 1,
        "suite": "Task 25 Provider Connection Manager",
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "os_name": os_name(),
        },
        "outcome": "PASS" if result.wasSuccessful() else "FAIL",
        "summary": {
            "tests_run": result.testsRun,
            **counts,
        },
        "tests": records,
    }


def os_name() -> str:
    """Return a stable standard-library platform identifier."""
    return platform.system()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="output/task25-test-report.json",
        help="JSON report destination (default: output/task25-test-report.json)",
    )
    args = parser.parse_args(argv)

    repository_root = Path(__file__).resolve().parents[2]
    tests_directory = repository_root / "tests"
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(tests_directory),
        pattern="test_*.py",
    )
    runner = unittest.TextTestRunner(
        verbosity=2,
        resultclass=Task25TestResult,
    )
    result = runner.run(suite)
    report = build_report(result)
    destination = Path(args.output)
    if not destination.is_absolute():
        destination = repository_root / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    print(
        "Task 25 report: "
        f"{report['outcome']} — {report['summary']['tests_run']} tests; "
        f"{report['summary']['failed']} failures; {report['summary']['errors']} errors; "
        f"artifact={destination}"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
