# Provider Manager Guide (Task #25)

This guide describes the current implementation in this branch. It is not a claim that the full A–P checklist is complete.

## Components

- `connection_manager.py` — provider catalog discovery, registration, enabled/disabled state, modality contract checks, credential-presence checks, endpoint policy, and verified connection state.
- `routing_manager.py` — priority, free-credit-first, lowest-cost, healthiest-first, and round-robin selection; optional health, cost, credit, and quota managers; explicit fallback behavior.
- `credit_manager.py` — stores verified free-credit snapshots from an injected provider-specific probe and excludes stale/unknown balances from routing.
- `cost_policy.py` — paid-approval guard, required project budgets, idempotent usage reconciliation, and estimated-cost reservations.
- `quota_manager.py` — hourly/daily/monthly UTC quota windows and request reserve/commit/release.
- `health_manager.py` — bounded provider health history, staleness/latency checks, and eligibility.
- `retry_policy.py` — bounded backoff decisions and a concurrency-safe circuit breaker.
- `output_validator.py` — file size, extension, signature/basic content checks, and SHA-256 for supported artifacts.
- `provider_service.py` — integration facade for connection verification, health recording, routing, output checks, retry decisions, and usage finalization.

## Safety contracts

1. **No automatic purchases.** These modules do not purchase credits, start subscriptions, or infer paid approval.
2. **Paid usage requires both explicit provider approval and a configured project budget.** Reserve the estimate before execution. After an actual provider request starts, reconcile the actual cost and commit its quota even if the generation failed.
3. **Free credits must be evidence-based.** Configure `ProviderCreditManager` with an injected, provider-specific account-balance probe. The probe must return a verified balance, source, unit, and observation time. No generic injected probe is proof of a live provider account balance.
4. **Do not release reservations just because a provider failed.** A failed API request may still consume quota or cost. `select_fallback` keeps reservations by default and requires a new attempt ID when a previous attempt may have started. Set `release_reservations=True` only when the caller knows the provider request did not start or could not be billed.
5. **Connectivity checks are injected.** `ConnectionManager.verify_connectivity` passes a timeout to the probe, but arbitrary Python callbacks cannot be forcibly stopped if they ignore it. Each provider-specific probe must enforce its own network timeout.
6. **Output validation is not full decoding.** Signatures and basic content checks detect many mismatches, but do not replace media decoding, malware scanning, or semantic quality checks.
7. **Persistence limits.** JSON files use atomic replacement and managers use process-local locks. These locks do not coordinate independent OS processes sharing the same files; cross-process transactions still need a database or file-locking layer.
8. **Secrets stay outside the catalog.** Store environment-variable names or references, not credential values. Returned provider metadata and state views redact credential-like fields.

## Verification

The GitHub Actions workflow compiles provider modules/tests, parses provider JSON catalogs, and runs all unit/regression tests with Python 3.13:

```bash
python -m compileall -q projects/provider_store tests
python -c "import json; from pathlib import Path; [json.loads(p.read_text(encoding='utf-8')) for p in Path('projects/provider_store').glob('*.json')]"
python -m unittest discover -s tests -p "test_*.py" -v
```

Live provider API probes, OAuth/token refresh, complete multimodal adapters, end-to-end generation execution, cross-process reservation safety, and the full canonical A–P checklist remain separate verification requirements.
