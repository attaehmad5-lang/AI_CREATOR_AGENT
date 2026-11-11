"""Fail-closed GitHub Actions evidence gate for Master Job Engine release.

Only GitHub's LIVE workflow-run and job API can establish hosted CI results.
A workflow created with no assigned runner/steps can never count as PASS.
No paid providers or CI workflows are invoked by this read-only script.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

REPO = "attaehmad5-lang/AI_CREATOR_AGENT"
BRANCH = "feature/master-job-engine-hardening-20261110"
WORKFLOW = "Master Job Engine Offline Regression"
EXPECTED_JOBS = frozenset({
    "master-job-engine (ubuntu-latest, 3.11)",
    "master-job-engine (ubuntu-latest, 3.13)",
    "master-job-engine (windows-latest, 3.11)",
    "master-job-engine (windows-latest, 3.13)",
})
_SHA = re.compile(r"[0-9a-f]{40}\Z")


class ReleaseGateError(RuntimeError):
    """Unable to independently establish the requested CI PASS."""


def audit_hosted_run(run: dict[str, Any], jobs_response: dict[str, Any], *,
                     expected_sha: str) -> list[str]:
    """Return all missing/unsafe conditions; [] is a hosted-CI-only PASS.

    This only checks GitHub-hosted CI, not provider, device or production QA.
    """
    if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
        raise ReleaseGateError("Expected a full 40-character Git commit SHA")
    problems = []
    if not isinstance(run, dict) or not isinstance(jobs_response, dict):
        return ["Missing workflow or job evidence"]
    if run.get("head_sha") != expected_sha:
        problems.append("Workflow ran against a different commit")
    if run.get("name") != WORKFLOW:
        problems.append("Unexpected workflow name")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        problems.append("Workflow has no successful completion")
    jobs = jobs_response.get("jobs")
    if not isinstance(jobs, list) or type(jobs_response.get("total_count")) is not int:
        return problems + ["Missing or incomplete GitHub job collection"]
    if jobs_response["total_count"] != len(jobs):
        problems.append("Job evidence is not complete")
    seen = set()
    for job in jobs:
        if not isinstance(job, dict) or job.get("name") not in EXPECTED_JOBS:
            continue
        name = job["name"]
        if name in seen:
            problems.append("Duplicate matrix job: " + name)
        seen.add(name)
        steps = job.get("steps")
        if (job.get("status") != "completed" or job.get("conclusion") != "success"
                or not isinstance(job.get("runner_name"), str) or not job["runner_name"].strip()
                or not isinstance(steps, list) or len(steps) < 3
                or any(not isinstance(step, dict) or step.get("conclusion") != "success" for step in steps)):
            problems.append("Matrix job not executed successfully: " + name)
    for name in sorted(EXPECTED_JOBS - seen):
        problems.append("Missing matrix job: " + name)
    return problems


def _fetch(url: str, *, token: str | None = None) -> dict[str, Any]:
    if not url.startswith("https://api.github.com/repos/" + REPO + "/"):
        raise ReleaseGateError("Blocked untrusted GitHub API URL")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AI-Creator-Job-Release-Gate/1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(3 * 1024 * 1024 + 1)
            if len(raw) > 3 * 1024 * 1024:
                raise ReleaseGateError("Oversized API response")
            data = json.loads(raw)
            if type(data) is not dict:
                raise ReleaseGateError("Invalid GitHub API response")
            return data
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise ReleaseGateError("Unable to verify GitHub API evidence") from exc


def live_ci_check(expected_sha: str, *, branch: str = BRANCH,
                  fetch: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Verify the branch HEAD and a successful GitHub CI run with real steps."""
    if not isinstance(expected_sha, str) or not _SHA.fullmatch(expected_sha):
        raise ReleaseGateError("Invalid expected commit SHA")
    if branch != BRANCH:
        raise ReleaseGateError("Unknown protected feature branch")
    get = fetch or (lambda url: _fetch(url, token=os.environ.get("GITHUB_TOKEN")))
    root = "https://api.github.com/repos/" + REPO
    branch_info = get(root + "/git/ref/heads/" + branch)
    if not isinstance(branch_info, dict) or branch_info.get("object", {}).get("sha") != expected_sha:
        raise ReleaseGateError("GitHub branch HEAD differs from requested commit")
    runs = get(root + "/actions/workflows/master-job-engine.yml/runs?per_page=30")
    if not isinstance(runs, dict) or type(runs.get("total_count")) is not int or not isinstance(runs.get("workflow_runs"), list):
        raise ReleaseGateError("Missing GitHub workflow evidence")
    qualifying = [r for r in runs["workflow_runs"] if isinstance(r, dict) and
                  r.get("head_sha") == expected_sha and r.get("name") == WORKFLOW]
    if not qualifying:
        raise ReleaseGateError("No GitHub workflow run for current commit")
    qualifying.sort(key=lambda r: r.get("id", 0) if type(r.get("id")) is int else 0, reverse=True)
    failures = []
    for run in qualifying:
        run_id = run.get("id")
        if type(run_id) is not int or run_id < 1:
            continue
        job_data = get(root + "/actions/runs/" + str(run_id) + "/jobs?per_page=100")
        problems = audit_hosted_run(run, job_data, expected_sha=expected_sha)
        if not problems:
            return {"status": "PASS", "scope": "hosted_ci_only", "head_sha": expected_sha,
                    "workflow_run_id": run_id, "verified_matrix_jobs": len(EXPECTED_JOBS)}
        failures.extend(problems)
    raise ReleaseGateError("Hosted CI is not PASS: " + "; ".join(dict.fromkeys(failures)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only GitHub runner verification for the Master Job Engine")
    parser.add_argument("--sha", required=True, help="Full HEAD commit SHA (no shortened or guessed SHA)")
    args = parser.parse_args(argv)
    try:
        result = live_ci_check(args.sha)
    except ReleaseGateError as exc:
        print(json.dumps({"status": "BLOCKED", "scope": "hosted_ci_only", "reason": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
