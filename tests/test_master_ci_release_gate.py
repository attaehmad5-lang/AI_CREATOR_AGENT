"""Offline CI-release-evidence tests. Network access not required."""
import unittest

from agents.jobs.ci_release_gate import (
    BRANCH, EXPECTED_JOBS, REPO, WORKFLOW, ReleaseGateError,
    audit_hosted_run, live_ci_check,
)

SHA = "a" * 40
BASE = "https://api.github.com/repos/" + REPO


def job(name, *, conclusion="success", runner="Hosted Agent"):
    return {"name": name, "status": "completed", "conclusion": conclusion,
            "runner_name": runner, "steps": [
                {"name": "Checkout", "conclusion": "success"},
                {"name": "Python setup", "conclusion": "success"},
                {"name": "Run tests", "conclusion": "success"},
            ]}


def run(*, conclusion="success", sha=SHA):
    return {"id": 101, "name": WORKFLOW, "status": "completed",
            "head_sha": sha, "conclusion": conclusion}


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.jobs = {"total_count": 4, "jobs": [job(x) for x in sorted(EXPECTED_JOBS)]}

    def test_four_executed_platform_jobs_are_required(self):
        self.assertEqual(audit_hosted_run(run(), self.jobs, expected_sha=SHA), [])

    def test_zero_step_startup_failures_never_pass(self):
        zero = {"total_count": 4, "jobs": [dict(job(x), runner_name="", steps=[], conclusion="failure")
                for x in sorted(EXPECTED_JOBS)]}
        problems = audit_hosted_run(run(conclusion="failure"), zero, expected_sha=SHA)
        self.assertGreaterEqual(len(problems), 5)

    def test_missing_windows_fails(self):
        only_linux = [j for j in self.jobs["jobs"] if "ubuntu" in j["name"]]
        issues = audit_hosted_run(run(), {"jobs": only_linux, "total_count": 2}, expected_sha=SHA)
        self.assertTrue(any("windows-latest" in p for p in issues))

    def test_no_runner_fails_even_with_green_conclusion(self):
        self.jobs["jobs"][0]["runner_name"] = ""
        self.assertTrue(audit_hosted_run(run(), self.jobs, expected_sha=SHA))

    def test_no_executed_steps_fails(self):
        self.jobs["jobs"][0]["steps"] = []
        self.assertTrue(audit_hosted_run(run(), self.jobs, expected_sha=SHA))

    def test_wrong_sha_never_passes(self):
        self.assertTrue(audit_hosted_run(run(sha="b" * 40), self.jobs, expected_sha=SHA))

    def test_pending_skipped_failed_steps_not_pass(self):
        for value in ("skipped", "failure", None):
            with self.subTest(value=value):
                jobs = {"total_count": 4, "jobs": [job(x) for x in sorted(EXPECTED_JOBS)]}
                jobs["jobs"][0]["steps"][0]["conclusion"] = value
                self.assertTrue(audit_hosted_run(run(), jobs, expected_sha=SHA))

    def test_incomplete_job_pagination_fails_closed(self):
        self.jobs["total_count"] = 7
        self.assertTrue(audit_hosted_run(run(), self.jobs, expected_sha=SHA))

    def test_workflow_mismatch_fails(self):
        self.assertTrue(audit_hosted_run(dict(run(), name="different"), self.jobs, expected_sha=SHA))

    def test_head_and_successful_live_run_can_pass(self):
        urls = []
        def fake_get(url):
            urls.append(url)
            if "/git/ref/" in url:
                return {"object": {"sha": SHA}}
            if "/workflows/" in url:
                return {"total_count": 1, "workflow_runs": [run()]}
            if "/jobs?" in url:
                return self.jobs
            raise AssertionError(url)
        result = live_ci_check(SHA, fetch=fake_get)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["verified_matrix_jobs"], 4)
        self.assertEqual(len(urls), 3)

    def test_bad_branch_head_never_passes(self):
        with self.assertRaisesRegex(ReleaseGateError, "HEAD"):
            live_ci_check(SHA, fetch=lambda _: {"object": {"sha": "b" * 40}})

    def test_no_run_never_passes(self):
        def fake_get(url):
            if "/git/ref/" in url:
                return {"object": {"sha": SHA}}
            return {"total_count": 0, "workflow_runs": []}
        with self.assertRaisesRegex(ReleaseGateError, "No GitHub workflow"):
            live_ci_check(SHA, fetch=fake_get)

    def test_untrusted_sha_and_branch_rejected_without_fetch(self):
        for invalid in ("abcd", "A" * 40, "../escape", None):
            with self.assertRaises(ReleaseGateError):
                live_ci_check(invalid, fetch=lambda _: self.fail("unexpected network"))
        with self.assertRaisesRegex(ReleaseGateError, "Unknown protected"):
            live_ci_check(SHA, branch="main", fetch=lambda _: self.fail("unexpected network"))

    def test_malformed_job_collection_never_passes(self):
        for malformed in ({}, {"total_count": "4", "jobs": []}, {"total_count": 4, "jobs": "x"}):
            self.assertTrue(audit_hosted_run(run(), malformed, expected_sha=SHA))


if __name__ == "__main__":
    unittest.main()
