# Master Job Engine v2.5 — signed tenant API boundary

This feature adds an **opt-in Python standard-library WSGI application**,
`SignedTenantJobApp`, at `agents.jobs.tenant_gateway`. It does not start a
server, expose an unauthenticated port, run paid providers, create missing media,
or claim a production deployment. It is a server-side integration primitive.

## Security model

- Every tenant has three **different** 32+ byte secrets: `client_key` for
  submit/status/cancel/events, `producer_key` for callback submissions, and
  `receipt_key` for independent signed stage evidence. Inject through a managed
  secrets store and rotate with a documented per-tenant procedure; never commit,
  print or put secrets in command-line arguments.
- Every request is authenticated with a domain-separated HMAC-SHA256 over the
  exact method, canonical route, tenant ID, role, 10–12 digit Unix timestamp,
  unique 16–80-character nonce, and SHA-256 of the request body. Header names:
  `X-AI-Tenant`, `X-AI-Role`, `X-AI-Timestamp`, `X-AI-Nonce`, `X-AI-Signature`.
  Signing and verification share `request_signature(...)`; HMAC comparison is
  constant time. Timestamp must be within five minutes of the server clock.
- A dedicated SQLite replay ledger durably stores only the SHA-256 of nonce
  values and tenant IDs. `BEGIN IMMEDIATE` plus a unique key rejects concurrent
  duplicated requests, including after server restart. Clients must use a **new
  nonce per HTTP attempt**, even for identical idempotency-key requests. Token
  replay within the acceptance window returns 409, not a second mutation.
- Tenant IDs are allowlisted ASCII identifiers and each tenant gets a separate
  SQLite job database and private artifact directory. Access checks do not
  query other tenants. Client/producer roles cannot perform each other's
  operations. Responses intentionally omit prompts, attached media, signing
  keys, provider operation IDs and request contents.
- Strict production mode always requires a stage receipt key and an existing
  artifact root. Callback HTTP authentication is **not** a substitute for the
  independent `stage_receipt_signature` value: the trusted production producer
  must verify the upstream provider's claim, ingest, scan and check actual media
  before issuing such a receipt.
- POST is limited to 256 KiB and JSON only. Duplicate keys, malformed JSON,
  NaN, unknown fields, unsanctioned paths and query strings fail closed.
- The WSGI boundary does **not** expose paid dispatch. The existing at-most-once
  external dispatch requires a separate authenticated, billing-approved service
  with provider-specific idempotency keys and actual budget enforcement.

## Entrypoint and routes

Instantiate from a trusted service process after loading per-tenant secrets:

```python
from agents.jobs.tenant_gateway import SignedTenantJobApp, TenantSecrets

app = SignedTenantJobApp(
    '/var/lib/ai-creator/master-job-tenants',
    {
        'tenant_example': TenantSecrets(
            client_key=client_key_from_secret_manager,
            producer_key=producer_key_from_secret_manager,
            receipt_key=receipt_key_from_secret_manager,
        )
    },
)
```

This snippet **defines** a WSGI app. It does not open a port. Deploy behind a
trusted TLS-capable WSGI server and correctly configured reverse proxy. The
trusted server must set `wsgi.url_scheme=https` only for a genuine HTTPS
request. Never trust a client-supplied `X-Forwarded-Proto` header directly.
By default, plain HTTP is refused; `allow_insecure_loopback=True` is for local
acceptance tests only. Terminate external HTTPS at a trusted proxy, isolate
upstream sockets from untrusted networks, restrict state directory permissions,
apply rate limiting, process limits and TLS certificates, and log **only** safe
request metadata. Do not expose the local CLI or tenant secrets to clients.

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/v1/jobs` | client | Submit request with idempotency key |
| GET | `/v1/jobs/{uuid}` | client | Redacted job status |
| GET | `/v1/jobs/{uuid}/events` | client | Metadata-only event stream (50 max) |
| POST | `/v1/jobs/{uuid}/cancel` | client | Request cancellation |
| POST | `/v1/jobs/{uuid}/stages/{STAGE}` | producer | Submit independently signed stage output |

The stage request JSON shape is `{"output": {...}, "receipt_signature":
"<64 hex chars>"}`. The producer must create the inner signature with the
**independent receipt key** using `stage_receipt_signature` and the exact
`job_id`, stage, and JSON output. Never give the receipt key to client apps.
Run worker processing under a separate privileged service account bound to
the same per-tenant database, secret and artifact root; a worker does not use
an HTTP client token. Never run producer credentials in an end-user app.

This architecture has **tenant** isolation, not per-user permissions *within*
a single tenant. Applications requiring individual user permissions must
implement them before issuing a tenant API key or place each separately
isolated account in a distinct tenant. The WSGI framework and upstream proxy
remain responsible for TLS, user login, abuse prevention, token revocation,
key rotation, request timeouts, rate limits and system monitoring.

## Offline acceptance (free, no cloud dependency)

```bash
python -m unittest discover -s tests -p 'test_master*.py' -v
python -m scripts.offline_tenant_api_acceptance
python -m scripts.offline_media_acceptance  # requires ffmpeg/ffprobe
```

The WSGI tests validate real signed requests, role separation, replay after
restart and across concurrent threads, independent tenant status and mutation
boundaries, media-stage signed evidence and private responses. The loopback
HTTP test sends genuine localhost TCP requests to a test-only HTTP server;
this is **not** a hosted TLS or live provider test. Run full Linux and Windows
CI and independent mobile-host tests before a production-ready declaration.

## Unresolved release gates

Real OAuth/OIDC/tenant login integration, server hardening against internet
traffic, independent provider sandbox API & billing attestation, actual media
security scanning, Windows hosted-runner CI, Android/iOS on-device validation,
external availability monitoring, and live production rollout remain separate
integration and operations activities requiring credentials/infrastructure.
The code cannot claim these checks PASS without execution evidence.
