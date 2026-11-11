# Master Job Engine — Hosted CI release evidence gate

Before calling any build a `Production PASS`, verify that its latest protected
feature-branch commit actually executed the **full Ubuntu/Windows matrix** on
GitHub runners. CI runs ending in `startup_failure` with zero steps are not
code failures, but **they are not CI PASS**. Do not merge solely on a local PASS.

The read-only `agents.jobs.ci_release_gate` checks GitHub's API for the branch
head, an actual workflow run at that exact commit, and four successfully
executed matrix jobs with nonempty runner names and three successful steps:

- Ubuntu / Python 3.11
- Ubuntu / Python 3.13
- Windows / Python 3.11
- Windows / Python 3.13

Use the exact 40-character commit SHA. It refuses stale run evidence,
incomplete pages, empty/unassigned runners, failed or skipped steps, and
unverified branches. It never triggers a workflow or spends provider credits.

```bash
python -m agents.jobs.ci_release_gate --sha 0123456789abcdef0123456789abcdef01234567
```

The SHA above is a formatting example, **not** an expected pass. If the
repository is private, configure a read-only `GITHUB_TOKEN` using the deployment
secret store. Do not log the token. Without GitHub API access the tool reports
`BLOCKED` and exits 2; it never substitutes fixture data for live evidence.

Tests of the gate use mocked API responses and do not make network calls:

```bash
python -m unittest discover -s tests -p 'test_master_ci_release_gate.py' -v
```

**Scope:** A successful result certifies only the hosted CI matrix. It is NOT
proof of Android/iOS hardware execution, live external provider billing
safety, real secret-store authentication, deployment TLS, multi-user production
security, media quality or backup restore under a disaster. Those still require
independent recorded live acceptance. Real GitHub Actions execution remains
blocked if billing/runner provisioning fails; this code does not bypass account
policies. Do not switch to costly runners or make a private repository public
merely to turn a badge green.
