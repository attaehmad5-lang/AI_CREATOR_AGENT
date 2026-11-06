# Task 25 — Provider Endpoint and Network Security Threat Model

**Status:** Code-level controls strengthened; deployment review and complete outbound-client integration remain open.  
**Scope:** Feature #71 — Provider Endpoint Allowlisting and Network Security.

## 1. Assets and security objectives

The provider integration boundary can process provider credentials, prompts, generated-content references, tenant identifiers, and usage metadata. The primary objectives are to prevent a caller-controlled endpoint from reaching internal services, prevent credentials from being sent to an unintended host, preserve TLS peer identity, prevent redirect-based policy bypass, and bound network resource consumption.

## 2. Trust boundaries and attacker capabilities

| Boundary | Treat as untrusted | Required control |
|---|---|---|
| API caller → provider service | Endpoint strings, model/options metadata, provider identifiers | Admin authorization for configuration; schema validation; sensitive-data scanning |
| Provider catalog → connection manager | Hostnames, endpoint metadata, enabled-provider flags | Exact hostname allowlist; no wildcard entries; HTTPS and URL parsing checks |
| DNS resolver → outbound socket | A/AAAA records and changing DNS answers | Reject any non-global address; pin the selected numeric address for the connection |
| TLS peer → probe | Certificate, status, headers and body | Certificate verification, SNI for the original hostname, TLS 1.2 minimum, no redirects |
| Credential store → HTTP request | Secret values and leases | Explicit auth-protected endpoint configuration, constrained auth-header names, short-lived lease and cleanup |
| Provider response → application | Status, retry headers and body size | Status validation, bounded read, response/connection cleanup |

Assume an attacker can submit malformed endpoint strings, operate a domain with changing DNS answers, control a redirect response, or return oversized and malformed responses. The attacker must not be able to modify the trusted deployment catalog or inject arbitrary Python transport objects; those are privileged application/deployment capabilities.

## 3. Implemented code-level controls

### Endpoint and catalog validation

- Provider catalogs accept explicit hostnames, not wildcard host patterns.
- Endpoint parsing rejects unsupported schemes, missing hosts, invalid ports, embedded user credentials, query parameters, fragments, and hosts outside the exact normalized allowlist.
- Private, loopback, link-local, reserved and metadata-service literal IP addresses are rejected for normal API endpoints even when someone adds that literal address to the host allowlist.
- The separate Ollama local adapter remains loopback-only and is not routed through the generic external-provider probe.

### DNS and connection binding

- The generic HTTPS probe resolves the allowlisted hostname before connecting.
- The complete A/AAAA answer set is inspected. A mixed public/private response fails closed; non-global and scoped IP addresses are rejected.
- The connection is made to the selected numeric IP address, not by resolving the hostname a second time. This removes the application-level DNS check/connect gap for the probe transport.
- TLS still verifies the certificate against the original provider hostname and sends that hostname as SNI.
- DNS answer count is bounded. A failed address attempt may move only to another address from the same already-validated DNS answer set.

### HTTP, TLS and credential handling

- The default transport is direct HTTPS and does not inherit ambient HTTP proxy configuration. A trusted custom opener is a privileged transport override and is not suitable for untrusted callers.
- TLS certificate verification remains enabled and the default transport requires TLS 1.2 or newer.
- Redirects are never followed automatically. A 3xx response is an error.
- Request timeouts and response reads are bounded. Oversized response bodies are rejected, and the response and connection are closed.
- Credentialed probes require an explicit endpoint configuration documenting that the endpoint validates the credential. Header names and schemes are restricted; credential leases and request headers are cleared after use.
- Probe success indicates transport/authentication reachability only. It does not establish generation capability, account credit, provider policy compliance, or output quality.

## 4. Regression evidence

The Task 25 test workflow adds coverage for:
- private IP rejection even if explicitly listed as an allowed host;
- private DNS answers and mixed public/private answer sets failing before socket connection;
- use of the already-validated numeric address for the outbound socket while retaining the provider hostname for TLS identity;
- exact allowlist matching;
- oversized response rejection and cleanup; and
- existing credential, redirect, TLS, timeout, header-injection, and response-cleanup behavior.

## 5. Residual risks and deployment requirements

This document is a repository-level threat model, not a review of a live deployment. The following gates remain:

1. The operating-system resolver call itself does not expose a portable per-call DNS timeout through the current standard-library interface. Production deployments should use a bounded, monitored resolver implementation or enforce DNS query timeouts at the resolver/network layer.
2. The pinned transport protects the generic connectivity probe. It does not automatically wrap every provider generation SDK, user-supplied adapter, subprocess, or third-party HTTP client. Those outbound paths still require inventory and adapter-level endpoint/e­gress enforcement.
3. Production network egress controls should independently block loopback, private, link-local, reserved and cloud metadata ranges except for explicitly approved self-hosted services. Provider allowlists are not a replacement for firewall or cloud network policy.
4. A trusted custom opener/resolver can change transport behavior. Only application-owned, reviewed transports should be injected; never accept these objects from API callers or project data.
5. DNS authenticity, resolver compromise, provider account permissions, provider-side abuse handling and deployment-specific proxy/firewall rules are outside this unit's proof.
6. The configuration-management API must remain behind the admin-authorized ProviderService boundary. Direct ConnectionManager access is an internal privileged capability, not an API permission boundary.

**Disposition:** Feature #71 remains **PARTIAL** until every outbound provider client is covered by a reviewed egress policy and the deployment resolver/firewall controls are evidenced. The generic probe controls above are not a blanket guarantee for arbitrary adapters or all outbound requests.
