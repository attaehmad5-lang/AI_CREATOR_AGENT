# Parallel Development Engine (APDE)

## Scope
This package is the first orchestration foundation for AI_CREATOR_AGENT's
parallel development workflow. It runs caller-supplied work items concurrently,
respects a dependency DAG, applies a hard concurrency ceiling, retries transient
exceptions, and skips work whose dependencies failed.

## Safety and scale
- Hard maximum worker limit: 500.
- Default concurrency: 5; increase only after load tests and provider/API quota checks.
- Workers execute only the callable supplied by the integrating application.
- No shell, GitHub write, deployment, or autonomous code-edit capability is granted.
- Failed dependencies block downstream tasks.
- Dependency cycles and unknown dependencies are rejected before execution.

## Integration
Import `ParallelDevelopmentEngine`, `EngineConfig`, and `WorkItem`.
Supply an async executor and explicit worker/retry configuration. Keep independent
workspaces/branches per coding worker; serialize shared-file integration and require
tests/review before merging.

## Validation
Run:
```bash
python -m unittest tests.test_parallel_development_engine -v
```

This foundation does not claim 500 live AI agents or 500x speed. It provides
bounded task scheduling that can be load-tested and connected to approved agent
providers in later increments.
