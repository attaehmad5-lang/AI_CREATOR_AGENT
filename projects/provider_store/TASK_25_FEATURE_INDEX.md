# Task #25 — Provider Connection Manager: Feature Index

**Use:** durable feature-to-task index for Task #25. The 138 numbered items are grouped A–P; numbers and names are not to be merged or renumbered.

## Status integrity warning

The user's original baseline aggregate was **138 total: 6 🟢, 29 🟡, 103 🔴**. The latest recovered per-item notes currently produce **6 🟢, 65 🟡, 67 🔴**, so they are not the same snapshot. The marker column below records the **latest recovered triage marker**, not a claim that it reproduces every original baseline marker verbatim. Do not overwrite the original baseline or infer an item's initial marker from the aggregate. The verbatim original pasted list should be compared against this index before the baseline markers are locked.

Every feature's **verification status is NOT ITEM-AUDITED** until the exact requirement is matched to implementation evidence and a relevant test. A group/module existing is not proof that each feature is complete.

## Per-feature workflow

**Scope → Build → Test → Fix failures → Rerun → Verify → Save evidence.** Failed test runs remain in history; a fix is only verified after a successful rerun. Preserve secrets, never make paid calls without explicit approval, and never infer that a balance is free credit.

## Feature list

| ID | Section | Feature name | Latest recovered triage marker | Verification status | Evidence / test reference |
|---:|---|---|:---:|---|---|
| 1 | A — Provider Registry & Lifecycle | Provider Registry | 🟡 | TESTED FOUNDATION; original wording comparison pending | provider_catalog.json; tests/test_provider_catalogs.py::test_all_provider_catalog_json_files_pass_schema_validation; tests/test_connection_manager.py::test_loads_catalog_and_unknown_state |
| 2 | A — Provider Registry & Lifecycle | Provider Registration | 🟡 | TESTED FOUNDATION; original wording comparison pending | tests/test_connection_manager.py::test_register_provider_persists_valid_metadata; test_register_provider_rejects_duplicate_without_overwrite; test_register_provider_rejects_missing_contract_fields |
| 3 | A — Provider Registry & Lifecycle | Provider Discovery | 🟡 | TESTED FOUNDATION; original wording comparison pending | tests/test_connection_manager.py::test_discovery_filters_by_category_case_insensitively; test_discovery_filters_by_capability; test_discovery_enabled_only_excludes_unspecified_and_disabled |
| 4 | A — Provider Registry & Lifecycle | Provider Loading | 🟡 | TESTED FOUNDATION; original wording comparison pending | tests/test_connection_manager.py::test_load_provider_uses_factory_and_keeps_instance_in_memory; test_load_provider_rejects_disabled_provider; test_load_provider_rejects_missing_factory_and_unknown_provider |
| 5 | A — Provider Registry & Lifecycle | Provider Unloading | 🟡 | TESTED FOUNDATION; original wording comparison pending | tests/test_connection_manager.py::test_unload_provider_calls_close_and_removes_instance; test_unload_provider_preserves_instance_if_cleanup_fails; test_unload_provider_reports_not_loaded_and_unknown |
| 6 | A — Provider Registry & Lifecycle | Provider Enable/Disable | 🟡 | TESTED FOUNDATION; original wording comparison pending | tests/test_connection_manager.py::test_enable_disable_persists_catalog_flag_without_claiming_connection; test_disabling_unloads_runtime_instance; test_enabled_flag_rejects_non_boolean_and_unknown_provider |
| 7 | A — Provider Registry & Lifecycle | Provider Metadata | 🟡 | TESTED FOUNDATION; original wording comparison pending | tests/test_connection_manager.py::test_metadata_returns_detached_copy_and_filters_sensitive_fields; test_catalog_status_is_not_exposed_as_live_connection_status; test_metadata_returns_none_for_unknown_provider |
| 8 | A — Provider Registry & Lifecycle | Provider Interface Contract | 🟡 | TESTED FOUNDATION; original wording comparison pending | tests/test_connection_manager.py::test_provider_contract_accepts_required_modality_method; test_provider_contract_rejects_missing_method_and_prevents_loading; test_provider_contract_fails_closed_when_method_attribute_raises |
| 9 | B — Credentials & Connection | API Key Management | 🟡 | TESTED FOUNDATION; provider-specific live verification pending | tests/test_connection_manager.py::test_credential_check_never_returns_secret; test_catalog_provider_credentials_resolve_from_environment_references; test_custom_api_key_and_credential_env_references_are_supported |
| 10 | B — Credentials & Connection | Token Expiry Detection | 🟡 | TESTED FOUNDATION | tests/test_connection_manager.py::test_credential_expiry_detection_never_exposes_token; test_credential_expiring_soon_is_flagged; test_connectivity_never_probes_with_expired_credentials; tests/test_oauth_pkce_manager.py::test_near_expiry_access_token_is_not_returned_for_new_requests |
| 11 | B — Credentials & Connection | Connection Verification | 🟡 | TESTED FOUNDATION; live provider probe pending | tests/test_connection_manager.py::test_connectivity_probe_marks_connected_only_after_success; test_public_state_setter_cannot_mint_verified_connection; tests/test_provider_service.py::test_verify_provider_updates_connection_and_health_together |
| 12 | B — Credentials & Connection | OAuth/Login Integration | 🔴 | PARTIAL; generic PKCE and Pollinations helper tested, live OAuth registration/scope verification pending | tests/test_oauth_pkce_manager.py::test_pollinations_helper_uses_official_pkce_endpoints_and_scopes; test_code_exchange_uses_verifier_and_keeps_tokens_out_of_status |
| 13 | B — Credentials & Connection | Credential Refresh | 🟡 | PARTIAL; explicit refresh helper tested, provider-specific refresh integration pending | tests/test_oauth_pkce_manager.py::test_refresh_rotates_access_and_refresh_tokens; test_refresh_is_explicitly_unsupported_without_refresh_token |
| 14 | B — Credentials & Connection | Endpoint Configuration | 🟡 | TESTED FOUNDATION | tests/test_connection_manager.py::test_configured_endpoint_and_model_are_passed_to_probe; test_provider_model_configuration_is_persisted_without_credentials; test_provider_endpoint_configuration_rejects_unsafe_or_secret_urls |
| 15 | B — Credentials & Connection | HTTPS & Secure Endpoint Validation | 🟡 | TESTED FOUNDATION; production certificates/live hosts not exercised | tests/test_connection_manager.py::test_endpoint_validation_requires_https_and_blocks_embedded_credentials; test_endpoint_validation_enforces_allowlist_and_local_http_policy; tests/test_http_probe.py::test_redirects_are_rejected_and_http_error_response_is_closed |
| 16 | B — Credentials & Connection | Connection Error Classification | 🟡 | TESTED FOUNDATION; live provider failure injection pending | tests/test_connection_manager.py::test_connectivity_probe_classifies_timeout_and_does_not_claim_connected; test_connectivity_probe_classifies_auth_and_network_failures_without_details; tests/test_http_probe.py::test_authentication_http_status_is_preserved_for_non_retryable_decisions |
| 17 | C — Provider Modalities | Image Generation Integration | 🔴 | OPEN; catalog contract is not a working generator adapter | tests/test_provider_catalogs.py::test_enabled_providers_have_explicit_generation_capabilities; test_every_catalog_modality_has_a_callable_contract |
| 18 | C — Provider Modalities | Video Generation Integration | 🔴 | OPEN; catalog contract is not a working generator adapter | tests/test_provider_catalogs.py::test_every_catalog_modality_has_a_callable_contract; real video-generation adapter and end-to-end generation test not present |
| 19 | C — Provider Modalities | Voice Generation Integration | 🔴 | OPEN; catalog contract is not a working generator adapter | tests/test_provider_catalogs.py::test_every_catalog_modality_has_a_callable_contract; real voice/audio adapter and end-to-end generation test not present |
| 20 | C — Provider Modalities | Text Generation Integration | 🔴 | OPEN; catalog contract is not a working generator adapter | tests/test_provider_catalogs.py::test_every_catalog_modality_has_a_callable_contract; real text/script adapter and end-to-end generation test not present |
| 21 | C — Provider Modalities | Music Generation Integration | 🔴 | OPEN; catalog contract is not a working generator adapter | tests/test_provider_catalogs.py::test_every_catalog_modality_has_a_callable_contract; real music-generation adapter and end-to-end generation test not present |
| 22 | C — Provider Modalities | Editing Integration | 🔴 | OPEN; catalog contract is not a working generator adapter | tests/test_provider_catalogs.py::test_every_catalog_modality_has_a_callable_contract; editing/upscaling/restoration adapter not present |
| 23 | C — Provider Modalities | Transcription Integration | 🔴 | OPEN; catalog contract is not a working generator adapter | tests/test_provider_catalogs.py::test_every_catalog_modality_has_a_callable_contract; transcription/translation/dubbing adapter not present |
| 24 | C — Provider Modalities | Modality Interface Contracts | 🟡 | TESTED FOUNDATION; actual provider modality adapters remain open | tests/test_connection_manager.py::test_provider_contract_accepts_required_modality_method; test_provider_contract_rejects_missing_method_and_prevents_loading; tests/test_provider_catalogs.py::test_every_catalog_modality_has_a_callable_contract |
| 25 | D — Routing & Fallback | Primary Provider Selection | 🟡 | TESTED FOUNDATION; full canonical-item comparison pending | tests/test_routing_manager.py::test_primary_route_skips_disabled_and_orders_by_priority |
| 26 | D — Routing & Fallback | Fallback Provider Selection | 🟡 | TESTED FOUNDATION; generation-path fallback integration pending | tests/test_routing_manager.py::test_fallback_excludes_failed_provider; test_quota_manager_blocks_over_limit_provider_and_uses_fallback |
| 27 | D — Routing & Fallback | PRIMARY_THEN_FALLBACK Strategy | 🟡 | TESTED FOUNDATION; orchestration integration pending | tests/test_routing_manager.py::test_primary_route_skips_disabled_and_orders_by_priority; test_fallback_excludes_failed_provider |
| 28 | D — Routing & Fallback | PRIMARY_ONLY Strategy Testing | 🔴 | TESTED FOUNDATION; no automatic paid operation is performed | tests/test_routing_manager.py::test_primary_only_strategy_never_selects_fallback_after_primary_rejected |
| 29 | D — Routing & Fallback | Automatic Fallback On/Off Testing | 🟡 | PARTIAL; explicit fallback is tested, automatic fallback on/off toggle is not integrated | tests/test_routing_manager.py::test_fallback_excludes_failed_provider; test_fallback_releases_and_reassigns_budget_and_quota_reservations |
| 30 | D — Routing & Fallback | Capability-Based Routing | 🔴 | TESTED FOUNDATION; real modality adapters remain open | tests/test_routing_manager.py::test_capability_based_routing_selects_only_matching_provider; tests/test_connection_manager.py::test_discovery_filters_by_capability |
| 31 | D — Routing & Fallback | Model/Version-Aware Routing | 🔴 | TESTED FOUNDATION; requires explicit configured model/version | tests/test_connection_manager.py::test_provider_model_configuration_is_persisted_without_credentials; tests/test_routing_manager.py::test_model_and_version_aware_routing_requires_exact_configured_match |
| 32 | D — Routing & Fallback | User/Project Policy-Based Routing | 🔴 | PARTIAL; project budgets and approvals are checked, per-project provider selection policy is not implemented | tests/test_routing_manager.py::test_cost_policy_blocks_paid_route_without_budget_or_approval; test_paid_budget_routing_rejects_incompatible_cost_unit |
| 33 | E — Retry, Failure & Recovery | Configurable Retry Limit | 🟢 | TESTED FOUNDATION | tests/test_retry_policy.py::test_timeout_is_retryable_but_respects_attempt_limit; test_invalid_policy_configuration_is_rejected |
| 34 | E — Retry, Failure & Recovery | Error-Specific Retry Policy | 🟡 | TESTED FOUNDATION | tests/test_retry_policy.py::test_auth_and_nonretryable_errors_are_not_retried; test_http_status_and_retry_after_are_read_from_exception |
| 35 | E — Retry, Failure & Recovery | Exponential Backoff and Jitter | 🔴 | TESTED FOUNDATION | tests/test_retry_policy.py::test_retries_transient_server_failure_with_exponential_backoff |
| 36 | E — Retry, Failure & Recovery | HTTP 429/Rate-Limit Handling | 🔴 | TESTED FOUNDATION | tests/test_retry_policy.py::test_http_429_respects_retry_after; tests/test_http_probe.py::test_rate_limit_metadata_reaches_connection_manager_safely |
| 37 | E — Retry, Failure & Recovery | Quota Exhaustion Detection | 🟡 | TESTED FOUNDATION | tests/test_routing_manager.py::test_quota_manager_blocks_over_limit_provider_and_uses_fallback; tests/test_quota_manager.py |
| 38 | E — Retry, Failure & Recovery | Circuit Breaker | 🔴 | TESTED FOUNDATION | tests/test_retry_policy.py::test_circuit_breaker_opens_at_threshold_and_recovers_after_cooldown; test_circuit_breaker_admits_only_one_concurrent_half_open_probe |
| 39 | E — Retry, Failure & Recovery | Cooldown and Provider Recheck | 🔴 | TESTED FOUNDATION | tests/test_retry_policy.py::test_circuit_breaker_opens_at_threshold_and_recovers_after_cooldown; test_circuit_breaker_reopens_after_half_open_failure |
| 40 | E — Retry, Failure & Recovery | Timeout, Cancellation and Stalled-Request Handling | 🔴 | PARTIAL; timeout/retry and circuit-breaker cancellation are tested, generation job cancellation/stall handling is not integrated | tests/test_retry_policy.py::test_timeout_is_retryable_but_respects_attempt_limit; test_cancelled_half_open_probe_releases_single_probe_slot |
| 41 | F — Provider Health | Provider Health State Tracking | 🟢 | TESTED FOUNDATION | tests/test_health_manager.py::test_success_and_failure_history_persist; test_concurrent_health_updates_across_processes_preserve_history |
| 42 | F — Provider Health | Basic Success/Failure History | 🟢 | TESTED FOUNDATION | tests/test_health_manager.py::test_success_and_failure_history_persist; test_request_id_makes_health_recording_idempotent_and_conflict_safe |
| 43 | F — Provider Health | Health Check Before Generation | 🟡 | PARTIAL; health evidence can be required by routing, generation-path enforcement remains open | tests/test_routing_manager.py::test_health_evidence_can_be_required_and_unhealthy_providers_are_skipped |
| 44 | F — Provider Health | Configurable Health-Check Policy | 🟡 | TESTED FOUNDATION | tests/test_health_manager.py::test_failure_threshold_excludes_unhealthy_provider; test_latency_limit_excludes_slow_provider; test_invalid_manager_configuration_and_probe_timeout_are_rejected |
| 45 | F — Provider Health | Active Health Probes | 🔴 | TESTED FOUNDATION; provider-specific live probes still need authorized credentials | tests/test_health_manager.py::test_active_probe_classifies_failure_without_leaking_details; test_active_probe_fails_closed_when_health_state_cannot_be_saved |
| 46 | F — Provider Health | Unhealthy Provider Exclusion | 🟡 | TESTED FOUNDATION | tests/test_health_manager.py::test_failure_threshold_excludes_unhealthy_provider; tests/test_routing_manager.py::test_health_evidence_can_be_required_and_unhealthy_providers_are_skipped |
| 47 | F — Provider Health | Provider Recovery Detection | 🔴 | TESTED FOUNDATION | tests/test_health_manager.py::test_success_recovers_provider |
| 48 | F — Provider Health | Stale Health and Consistency Checks | 🔴 | TESTED FOUNDATION | tests/test_health_manager.py::test_stale_health_is_excluded; test_future_health_evidence_and_malformed_latency_fail_closed; test_history_is_bounded |
| 49 | G — Output Validation | Image Output Validation | 🟢 | TESTED FOUNDATION for signature/hash checks; image semantic/dimension validation remains open | tests/test_output_validator.py::test_valid_image_signature_returns_hash_and_metadata; test_mismatched_signature_and_extension_are_rejected |
| 50 | G — Output Validation | Output File Existence Validation | 🟡 | TESTED FOUNDATION | tests/test_output_validator.py::test_missing_empty_and_oversized_outputs_are_rejected |
| 51 | G — Output Validation | Audio/Video Format, Codec and Integrity Validation | 🔴 | OPEN; audio/video decoding, codec and integrity validators are not implemented | tests/test_output_validator.py::test_symlinks_and_unsupported_modalities_fail_closed |
| 52 | G — Output Validation | Corrupt/Incomplete Output Rejection | 🟡 | TESTED FOUNDATION for supported formats | tests/test_output_validator.py::test_mismatched_signature_and_extension_are_rejected; test_text_json_and_pdf_content_are_checked; test_deeply_nested_json_is_rejected_without_crashing |
| 53 | G — Output Validation | Temporary File Cleanup and Atomic Replacement | 🟡 | OPEN; validator does not own generation temp-file lifecycle/atomic replacement | tests/test_output_validator.py does not exercise temp-file creation or atomic output replacement |
| 54 | G — Output Validation | Previous Output Preservation on Failure | 🟡 | OPEN; prior-output preservation needs execution pipeline integration | No end-to-end generation/output replacement test exists in the current provider suite |
| 55 | G — Output Validation | File Size, Dimensions, Duration and Codec Constraints | 🔴 | PARTIAL; file-size limits exist, image dimensions/audio-video duration/codecs are not checked | tests/test_output_validator.py::test_missing_empty_and_oversized_outputs_are_rejected; test_invalid_size_limits_are_rejected |
| 56 | G — Output Validation | Provider Attribution and Output Metadata | 🟡 | PARTIAL; safe metadata/hash exists, provider attribution is not end-to-end | tests/test_output_validator.py::test_valid_image_signature_returns_hash_and_metadata; tests/test_provider_service.py output validation tests |
| 57 | H — Cost, Credits & Budgets | Free-Credit-First Routing | 🟡 | PARTIAL; verified free-credit routing is tested, actual provider free-tier eligibility remains unverified | tests/test_routing_manager.py::test_verified_credit_manager_drives_free_first_routing; tests/test_credit_manager.py::test_stale_or_unverified_balances_never_authorize_free_routing |
| 58 | H — Cost, Credits & Budgets | Provider Free-Tier/Quota Verification | 🔴 | OPEN; Pollinations combined pollen budget is not proof of free credits | projects/provider_store/pollinations_credit_probe.py returns remaining_account_budget only and cannot authorize free-credit routing |
| 59 | H — Cost, Credits & Budgets | Explicit Approval Before Paid Usage | 🟡 | TESTED FOUNDATION | tests/test_cost_policy.py::test_paid_usage_is_blocked_without_explicit_approval; test_paid_usage_requires_budget_and_approval |
| 60 | H — Cost, Credits & Budgets | Per-Request/Project Budgets | 🟡 | TESTED FOUNDATION | tests/test_cost_policy.py::test_project_budget_is_hard_stop; test_budget_reservations_prevent_overcommit_and_are_idempotent |
| 61 | H — Cost, Credits & Budgets | Estimated vs Actual Usage Reconciliation | 🟡 | TESTED FOUNDATION | tests/test_cost_policy.py::test_reconciliation_is_idempotent_and_persists_actual_usage; test_reconciliation_consumes_reservation_and_release_frees_budget |
| 62 | H — Cost, Credits & Budgets | Credit Exhaustion and Billing Error Handling | 🟡 | TESTED FOUNDATION; provider-specific billing webhooks remain open | tests/test_cost_policy.py::test_zero_actual_cost_requires_explicit_no_charge_evidence; test_unknown_charge_outcome_does_not_free_quota_for_another_request; tests/test_credit_manager.py::test_probe_failures_do_not_leak_exception_details |
| 63 | H — Cost, Credits & Budgets | Cost-Aware Provider/Model Selection | 🟡 | TESTED FOUNDATION | tests/test_routing_manager.py::test_free_credit_and_lowest_cost_strategies |
| 64 | H — Cost, Credits & Budgets | Budget Alerts and Hard-Stop Policy | 🟡 | TESTED FOUNDATION | tests/test_cost_policy.py::test_project_budget_is_hard_stop; test_actual_cost_overrun_triggers_budget_alert |
| 65 | I — Security & Privacy | Secrets Management | 🟡 | NOT ITEM-AUDITED | TBD |
| 66 | I — Security & Privacy | Secret Leakage Prevention | 🟡 | NOT ITEM-AUDITED | TBD |
| 67 | I — Security & Privacy | Endpoint Allowlisting | 🟡 | NOT ITEM-AUDITED | TBD |
| 68 | I — Security & Privacy | Least-Privilege Access | 🔴 | NOT ITEM-AUDITED | TBD |
| 69 | I — Security & Privacy | Permission Enforcement | 🔴 | NOT ITEM-AUDITED | TBD |
| 70 | I — Security & Privacy | Sensitive Data Protection | 🔴 | NOT ITEM-AUDITED | TBD |
| 71 | I — Security & Privacy | Data Retention Policies | 🔴 | NOT ITEM-AUDITED | TBD |
| 72 | I — Security & Privacy | Security Audit Logs | 🔴 | NOT ITEM-AUDITED | TBD |
| 73 | J — Jobs & Execution | Unique Job IDs | 🔴 | NOT ITEM-AUDITED | TBD |
| 74 | J — Jobs & Execution | Duplicate Request Prevention | 🟡 | NOT ITEM-AUDITED | TBD |
| 75 | J — Jobs & Execution | Full Idempotency | 🔴 | NOT ITEM-AUDITED | TBD |
| 76 | J — Jobs & Execution | Concurrency Safety | 🔴 | NOT ITEM-AUDITED | TBD |
| 77 | J — Jobs & Execution | Queue Integration | 🔴 | NOT ITEM-AUDITED | TBD |
| 78 | J — Jobs & Execution | Asynchronous Job Support | 🔴 | NOT ITEM-AUDITED | TBD |
| 79 | J — Jobs & Execution | Webhook Handling | 🔴 | NOT ITEM-AUDITED | TBD |
| 80 | J — Jobs & Execution | Cancellation & Shutdown Integration | 🔴 | NOT ITEM-AUDITED | TBD |
| 81 | K — Performance & Observability | Latency-Aware Routing | 🟡 | NOT ITEM-AUDITED | TBD |
| 82 | K — Performance & Observability | Provider Success/Error-Rate Metrics | 🟡 | NOT ITEM-AUDITED | TBD |
| 83 | K — Performance & Observability | Throughput, Queue-Wait and Timeout Metrics | 🔴 | NOT ITEM-AUDITED | TBD |
| 84 | K — Performance & Observability | Output Quality Validation/Score Integration | 🔴 | NOT ITEM-AUDITED | TBD |
| 85 | K — Performance & Observability | Task-Specific Quality Thresholds | 🔴 | NOT ITEM-AUDITED | TBD |
| 86 | K — Performance & Observability | Provider/Model Benchmarking | 🔴 | NOT ITEM-AUDITED | TBD |
| 87 | K — Performance & Observability | Structured Logs, Metrics and Distributed Traces | 🔴 | NOT ITEM-AUDITED | TBD |
| 88 | K — Performance & Observability | Alerts, Dashboards and Incident Diagnostics | 🔴 | NOT ITEM-AUDITED | TBD |
| 89 | L — Configuration & Recovery | Registry Schema Validation | 🟡 | NOT ITEM-AUDITED | TBD |
| 90 | L — Configuration & Recovery | Safe Configuration Defaults | 🟡 | NOT ITEM-AUDITED | TBD |
| 91 | L — Configuration & Recovery | Configuration Versioning | 🔴 | NOT ITEM-AUDITED | TBD |
| 92 | L — Configuration & Recovery | Configuration Migration | 🔴 | NOT ITEM-AUDITED | TBD |
| 93 | L — Configuration & Recovery | Hot Reload | 🔴 | NOT ITEM-AUDITED | TBD |
| 94 | L — Configuration & Recovery | Feature Flags | 🔴 | NOT ITEM-AUDITED | TBD |
| 95 | L — Configuration & Recovery | Backward Compatibility | 🔴 | NOT ITEM-AUDITED | TBD |
| 96 | L — Configuration & Recovery | Rollback & Backup/Restore | 🔴 | NOT ITEM-AUDITED | TBD |
| 97 | M — Integration & Reliability | Routing Helper Integration | 🟡 | NOT ITEM-AUDITED | TBD |
| 98 | M — Integration & Reliability | Provider Integration Coverage | 🟡 | NOT ITEM-AUDITED | TBD |
| 99 | M — Integration & Reliability | Primary-Only Regression Handling | 🟡 | NOT ITEM-AUDITED | TBD |
| 100 | M — Integration & Reliability | Retry/Backoff Integration | 🟡 | NOT ITEM-AUDITED | TBD |
| 101 | M — Integration & Reliability | Timeout Integration | 🟡 | NOT ITEM-AUDITED | TBD |
| 102 | M — Integration & Reliability | Mock Provider Fallback Integration | 🟢 | NOT ITEM-AUDITED | TBD |
| 103 | M — Integration & Reliability | Failure Simulation | 🟡 | NOT ITEM-AUDITED | TBD |
| 104 | M — Integration & Reliability | Invalid Output Handling | 🟡 | NOT ITEM-AUDITED | TBD |
| 105 | M — Integration & Reliability | Concurrency & Cancellation Integration | 🔴 | NOT ITEM-AUDITED | TBD |
| 106 | M — Integration & Reliability | Security/Privacy Integration | 🔴 | NOT ITEM-AUDITED | TBD |
| 107 | M — Integration & Reliability | End-to-End Modality Integration | 🔴 | NOT ITEM-AUDITED | TBD |
| 108 | M — Integration & Reliability | Failure Injection & Recovery | 🔴 | NOT ITEM-AUDITED | TBD |
| 109 | M — Integration & Reliability | Git Backup & Secret Exclusion | 🟡 | NOT ITEM-AUDITED | TBD |
| 110 | M — Integration & Reliability | Production Release Readiness | 🟡 | NOT ITEM-AUDITED | TBD |
| 111 | N — Advanced Routing | Multi-Factor Routing: Quality, Latency, Cost and Capability | 🟡 | NOT ITEM-AUDITED | TBD |
| 112 | N — Advanced Routing | Project-Specific Provider Selection | 🔴 | NOT ITEM-AUDITED | TBD |
| 113 | N — Advanced Routing | Automatic Exclusion of Providers That Cannot Meet Constraints | 🟡 | NOT ITEM-AUDITED | TBD |
| 114 | N — Advanced Routing | Confidence-Aware Routing and Uncertainty Handling | 🔴 | NOT ITEM-AUDITED | TBD |
| 115 | N — Advanced Routing | Controlled A/B Benchmarking Without Unintended Paid Usage | 🔴 | NOT ITEM-AUDITED | TBD |
| 116 | N — Advanced Routing | Adaptive Routing Using Verified Historical Performance | 🔴 | NOT ITEM-AUDITED | TBD |
| 117 | N — Advanced Routing | Region/Data-Residency-Aware Provider Selection | 🔴 | NOT ITEM-AUDITED | TBD |
| 118 | N — Advanced Routing | Human Approval for High-Cost or Sensitive Decisions | 🔴 | NOT ITEM-AUDITED | TBD |
| 119 | O — Usage & Management | Provider Usage Analytics | 🔴 | NOT ITEM-AUDITED | TBD |
| 120 | O — Usage & Management | Per-Provider Cost Analytics | 🔴 | NOT ITEM-AUDITED | TBD |
| 121 | O — Usage & Management | Usage Reports | 🔴 | NOT ITEM-AUDITED | TBD |
| 122 | O — Usage & Management | Provider Performance Reports | 🔴 | NOT ITEM-AUDITED | TBD |
| 123 | O — Usage & Management | User-Level Quotas | 🟢 | NOT ITEM-AUDITED | TBD |
| 124 | O — Usage & Management | Project/Tenant Usage Limits | 🟡 | NOT ITEM-AUDITED | TBD |
| 125 | O — Usage & Management | Provider-Level Spending Controls | 🔴 | NOT ITEM-AUDITED | TBD |
| 126 | O — Usage & Management | Usage Notifications | 🔴 | NOT ITEM-AUDITED | TBD |
| 127 | O — Usage & Management | Administrative Usage Controls | 🔴 | NOT ITEM-AUDITED | TBD |
| 128 | O — Usage & Management | Usage Policy Management | 🔴 | NOT ITEM-AUDITED | TBD |
| 129 | P — Extensibility & Administration | Plugin Provider Adapters | 🟡 | NOT ITEM-AUDITED | TBD |
| 130 | P — Extensibility & Administration | Versioned Provider Interface | 🟡 | NOT ITEM-AUDITED | TBD |
| 131 | P — Extensibility & Administration | Capability Discovery | 🟡 | NOT ITEM-AUDITED | TBD |
| 132 | P — Extensibility & Administration | Provider Onboarding | 🟡 | NOT ITEM-AUDITED | TBD |
| 133 | P — Extensibility & Administration | Backward Compatibility | 🟡 | NOT ITEM-AUDITED | TBD |
| 134 | P — Extensibility & Administration | Provider Contract Checks | 🟡 | NOT ITEM-AUDITED | TBD |
| 135 | P — Extensibility & Administration | Provider Documentation | 🟡 | NOT ITEM-AUDITED | TBD |
| 136 | P — Extensibility & Administration | Diagnostics | 🟡 | NOT ITEM-AUDITED | TBD |
| 137 | P — Extensibility & Administration | Local Model Provider Integration | 🔴 | NOT ITEM-AUDITED | TBD |
| 138 | P — Extensibility & Administration | Deprecation, Policy Simulation & Advanced Admin Controls | 🔴 | NOT ITEM-AUDITED | TBD |

## Group ranges

- A — Provider Registry & Lifecycle: 1–8
- B — Credentials & Connection: 9–16
- C — Provider Modalities: 17–24
- D — Routing & Fallback: 25–32
- E — Retry, Failure & Recovery: 33–40
- F — Provider Health: 41–48
- G — Output Validation: 49–56
- H — Cost, Credits & Budgets: 57–64
- I — Security & Privacy: 65–72
- J — Jobs & Execution: 73–80
- K — Performance & Observability: 81–88
- L — Configuration & Recovery: 89–96
- M — Integration & Reliability: 97–110
- N — Advanced Routing: 111–118
- O — Usage & Management: 119–128
- P — Extensibility & Administration: 129–138

## Current repository status

- PR #1 was merged by the user on 2026-10-12.
- Follow-up PR #3 is a draft and is not merged.
- Task #25 remains **IN PROGRESS**; do not mark all 138 features complete based on the overall unit-test count.
- Latest confirmed cross-platform suite at time of this index: run [#38285485587](https://github.com/attaehmad5-lang/AI_CREATOR_AGENT/actions/runs/38285485587), 304 tests passed on Ubuntu and 304 on Windows.
