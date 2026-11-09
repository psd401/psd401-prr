# Security Model and Review

This document records the application attack surface and the controls that
must stay true as the system evolves. It complements the legal/compliance
invariants in [`compliance-rules.md`](compliance-rules.md) and the integration
contract in
[`agentic-integration.md`](agentic-integration.md).

## Reviewed baseline

Verified 2026-07-22 by enumerating the Django URL resolver, authenticators,
permission classes, outbound HTTP call sites, deployment bindings, response
headers, dependency audits, and GitHub Actions workflows.

### Inbound surface

| Surface | Principal | Controls |
|---|---|---|
| SPA shell and static assets | Unauthenticated browser | Contains no request data; CSP, clickjacking protection, no indexing, and same-origin assets only. Issue #70 will make `/request` a purpose-built public page. |
| Staff login | Unauthenticated browser | Production uses Google OIDC authorization code flow with nonce and PKCE, exact callback URI, verified hosted domain/email, and immutable `sub`; local-only password login is CSRF-protected, throttled, and absent in Google mode |
| Public request intake | Unauthenticated browser | Exactly one write-only POST; same-origin CSRF, signed HttpOnly form token, minimum/maximum fill age, honeypot, Redis-backed per-IP and global hard ceilings, minimal PII, content-bounded quarantine attachments, one-request idempotency receipt, and a request-number-only success response |
| Staff API and API documentation | Staff session/token | Authenticated by default; sessions require CSRF; every resource declares a role policy; Liaison querysets are assignment-scoped; sensitive responses are `no-store` |
| Hosted attributed MCP at `/mcp` | Personal staff token or dedicated nonstaff service identity | Streamable HTTP preflights the caller's `Authorization: Token` through Django before handshake/tool execution, then forwards that same credential to the staff REST primitives; both identities may use the eight reads, while four role-gated record and decision-batch reads and exactly five gated draft/proposal tools require a personal token; current roles, expiry, revocation, attribution, resource policy, and human handoffs remain Django-owned; stateless workers, no token logging, and noncacheable responses |
| Stdio read-only MCP | Personal token or dedicated nonstaff service identity | Shared tool registry; the service identity has exact `records.read_staff_api`, safe HTTP methods only, and cannot submit intake, mutate staff resources, or read role mappings or the requester directory |
| In-process outbound requester correspondence | Migration-provisioned non-login system identity | Exact `correspondence.deliver_outbound_correspondence`; no token; row-locked lease/attempt, immutable Django-prepared MIME, claim-bound clean attachments, development recipient allowlist, idempotent result, and Gmail Message-ID reconciliation before any uncertain resend |
| In-process inbound requester correspondence | Migration-provisioned non-login system identity | Exact `correspondence.submit_inbound_correspondence`; no token; committed-before-I/O reservation plus immutable hashed transport alias for every generated quarantine write, one bounded `PutObject`, processing/cleanup leases, immutable raw evidence, safe MIME parsing, and deterministic Django matching |
| Django admin | Break-glass superuser | Disabled outside debug unless explicitly enabled; rejects ordinary staff and Application Admin accounts; not part of normal operations |

Hosted `/mcp` exposes exactly five attributed personal-token draft/proposal
tools; standalone stdio and the shared nonstaff MCP identity remain read-only.
None of the five can approve, queue, or send: the worst case is an editable
draft a Records Officer discards, or a decision batch a person rejects. The
four record and decision-batch reads also need a personal token, because they
belong to reviewer and Records Officer roles.

The local login route exists only in local mode. Public intake is the one
sanctioned anonymous business operation and exposes no read method: its success
body contains only the new request number. Google mode instead exposes the OIDC
protocol initiation/callback routes; neither returns application data. No
requester, request detail, status, receipt, schema, or operational data is
readable anonymously. A URL-resolver regression test fails if another API view
is added without an explicit authentication boundary or reviewed exception.

### Outbound surface

Staff authentication calls Google's fixed OIDC endpoints. Role revalidation
uses a separate credential with the Cloud Identity read-only scope and looks up
only the named user's direct membership in groups configured in
`GoogleGroupRoleMapping`; it does not enumerate group members. The keyless
credential receives the Workspace **Groups Reader** role once so future mapping
changes do not require per-group service-account membership or another Google
Admin action. That role can read directory-wide group membership, so its
compensating controls are an exact AWS workload-identity trust, no private key,
read-only scope, configured-group-only application calls, and fail-closed
sessions. It does not reuse the Vault service account, receive Groups Editor or
Groups Administrator, or use domain-wide delegation. Only direct user
membership counts. The middleware checks roles every 15 minutes and fails
closed after a 30-minute stale window.

## Deployment boundary

Outside local debug, settings fail closed unless `DJANGO_SECRET_KEY` is unique
and high entropy. HTTPS redirect, Secure session/CSRF cookies, HSTS, an
eight-hour browser-close session, same-origin referrers, and standard Django
password validators are enabled. Google membership is independently refreshed
inside that session. Wildcard production hosts, local authentication mode, and
missing Google client/group credentials are rejected. The container entrypoint
runs Django's deploy checks and aborts on error.

The deploy Compose file binds Gunicorn's Uvicorn ASGI workers to host loopback
and publishes no
database, Redis, MinIO, or MinIO-console ports. A trusted reverse proxy must:

1. terminate TLS with a valid certificate;
2. strip client-supplied forwarding headers;
3. set `X-Forwarded-Proto: https` itself; and
4. be in place before `DJANGO_TRUST_X_FORWARDED_PROTO=true` is enabled;
5. replace rather than trust a client-supplied `X-Forwarded-For`; DRF trusts
   exactly the rightmost address from the single Caddy hop for public-intake
   per-IP throttling;
6. enforce a bounded inbound request-body limit that covers the configured
   75 MiB RFC822 maximum plus multipart overhead (at least 80 MiB for the
   defaults); and
7. allow the shipped bounded 300-second Gunicorn processing window, with a
   slightly longer proxy upstream timeout.

Never expose Gunicorn/Uvicorn or the internal service network directly to a
district LAN or the internet.

`config/asgi.py` is the single public application. Its top-level lifespan runs
FastMCP's session manager, routes exact `/mcp` requests to the authenticated
streamable-HTTP app, and delegates every other request to Django. Hosted MCP is
stateless across the four workers. It performs staff reads and its five gated
draft/proposal operations in-process through Django's URL, middleware,
authentication, permission, and serializer stack rather than making a public
self-request that could deadlock the worker pool.
For that internal request, it mirrors Django's explicitly configured
`SECURE_PROXY_SSL_HEADER`; forwarded scheme headers remain ignored unless the
operator enabled the documented trusted-proxy contract. This keeps the auth
probe secure behind the separate Caddy container without trusting arbitrary
client headers.
FastMCP's DNS-rebinding allowlist is derived from Django's non-wildcard
`ALLOWED_HOSTS`; Caddy's existing catch-all proxy therefore needs no new route
or public port.

## Identity and secret separation

Provision the shared read-only MCP identity separately from personal staff
credentials:

```bash
python manage.py provision_mcp_readonly mcp-production
```

Migrations provision the Gmail workers' separate non-login system principals;
they have no tokens and must never be reused for an HTTP workflow. Migration
`records.0016_retire_legacy_integration` revokes the three former exact-scope
integration token categories and removes their obsolete permission.

Rotate the MCP credential with `--rotate`. Do not reuse it for a human staff
account or another workflow. Tokens belong in an encrypted credential store and
must travel only over HTTPS. They must never be placed in source control,
screenshots, logs, or query strings.

The hosted `/mcp` endpoint accepts either an attributed personal token or the
separately provisioned nonstaff MCP identity. The dedicated nonstaff service
identity may use hosted `/mcp` for the eight read tools; the five draft/proposal
tools require an attributed personal token. That service identity also remains
appropriate for shared stdio automation. Static-token hosted mode is not OAuth
and must not be represented as compatible with a remote-connector UI that
cannot set the required header.

Human staff identities are Google accounts mapped to stable application roles
through administrator-configured Workspace groups. Where a person legitimately
needs the system but belongs to no group that can be synced, an Application
Admin may instead record a direct grant naming that exact Workspace address,
its role, and a written reason an access review can read. Grants are additive:
they union with whatever the configured groups already say and can never take
a role away. They are audited, they expire all Google sessions on every change,
and deactivating one removes the role at the holder's next authorization
refresh, exactly as losing group membership does. Because a grant is an
application record rather than a Google fact, a Workspace directory outage
does not lock out the person holding it; everyone whose access depends on a
group still fails closed. The last active Application Admin cannot be removed
from either source. Google email is a mutable attribute; the signed `sub` is
the identity key. An existing local account is
never linked by matching email alone: a superuser must grant a one-use link
approval first. Mapping changes are audited and expire all Google sessions.
Application Admin controls configuration but cannot transition a request,
revise an estimate, or exclude a camera; legal authority belongs to Records
Officer. Intake Clerk is an explicit write-only intake role: it can create a
request through the guarded staff endpoint but cannot browse requester PII or
inherit workflow/configuration authority. Existing district Google 2-factor
policy remains the MFA boundary.

## Automated security gates

CI runs the application tests, Bandit at medium-or-higher severity,
`pip-audit`, and the production frontend dependency audit. Dependabot monitors
Python, npm, and GitHub Actions dependencies. Every third-party action is pinned
to an immutable commit, and the Claude comment workflow accepts triggers only
from repository owners, members, or collaborators.
Correspondence row-lock and idempotency race tests also run against PostgreSQL
16 in a dedicated CI job rather than relying on SQLite's locking behavior.

The regression suite also proves:

- anonymous callers cannot read application data, and their sole write path is
  the CSRF/token/time/honeypot/rate/idempotency-gated public intake endpoint;
- retired integration credentials cannot authenticate and the MCP credential
  cannot cross its read-only permission boundary;
- local login keeps CSRF, throttling, and enumeration resistance, while
  production rejects the local password path;
- invalid Google claims, unmapped group members, silent account linking,
  cross-role access, and stale role sessions fail closed;
- intake retries do not create duplicate legal requests;
- requester files are count/size/type bounded, checked for a matching file
  signature instead of trusting the multipart MIME label, content-hashed into
  the idempotency contract, stored as octet streams under generated quarantine
  keys, malware-scanned by a private ClamAV service, and unavailable unless the
  scan succeeds;
- clean intake-file downloads require a Records Officer session, expire after
  five minutes, force attachment disposition, omit storage keys, and create an
  audit event;
- outbound requester content/recipients freeze before a separately scoped
  transport can claim it; active claims are leased, attempts are append-only,
  successful evidence is idempotent, and uncertain delivery reconciles by the
  deterministic RFC Message-ID before resend;
- inbound RFC822 is streamed under a transport limit, stored privately before
  parsing, protected by exact integration scope/idempotency, and matched only
  by Django. Every raw or attachment object first receives a durable,
  generation-numbered reservation. Durable hashed transport aliases bind
  original and replacement credentials to the same canonical evidence. Each
  attempt uses one content-length-bounded S3 `PutObject`, not multipart upload;
  a committed stored generation makes normal/concurrent exact replay reuse its
  key without another write, while a crash before `stored_at` may repeat the
  exact single PUT to that same key. The durable processing lease is committed
  before each storage I/O. Django reacquires and rechecks the writer lock before
  the PUT. It holds that lock through the PUT and marks the generation stored
  in the same transaction. The lease is refreshed after parsing, after each
  attachment stage, and immediately before adoption. This closes cleanup races
  after database-lock loss or a late PUT, and receipt/attachment adoption
  remains atomic with the domain commit. Malformed evidence is retained with
  sanitized reason codes;
- inbound attachments use generated keys, sanitized filenames, byte-derived
  types, decode/count/size limits (including valid zero-byte decoded
  attachments), inert quarantine metadata, ClamAV plus size/digest
  revalidation, clean-only officer downloads, atomic verdict/audit commits,
  durable stale-scan recovery, and a persistent retryable ledger for
  post-verdict quarantine deletion. A separate leased sweep handles unadopted
  write reservations only after both the stale grace and processing lease
  expire: it heals an exact domain reference and its audit atomically instead
  of deleting it, otherwise physically removes the orphan with persisted
  backoff. Recovery audits omit keys, filenames, addresses, and message
  content. Embedded URLs are never fetched;
- unmatched content and attachments cannot bypass the officer-only inbound
  queue through generic correspondence/download APIs; matched association
  corrections are versioned, reasoned, IP-attributed, and audited;
- production secret, HTTPS, cookie, and host checks fail an unsafe deployment;
  and
- sensitive API/admin responses are not cacheable.

## Known production gates

These are not hidden behind a claim of production readiness:

- The district operator must apply `infra/terraform/google-identity`, complete
  `docs/google-identity-bootstrap.md`, configure initial role mappings, and run
  the documented login/removal acceptance check before pilot use.
- HSTS subdomain inclusion and preload remain disabled until the district
  confirms every hostname in the parent domain is permanently HTTPS-capable.
- Database-level append-only enforcement for the audit trail still needs a
  trigger or pgaudit. The automated PostgreSQL/audit backup timer is installed,
  manifest-marked uploads were observed through 2026-08-18, and the isolated
  verifier is implemented. The 2026-08-25 isolated restore drill is retained
  under `docs/operations/` and authorizes only the exact pinned additive
  migration set in that release; every other migration-bearing or
  data-destructive deployment needs its own reviewed evidence and recovery
  plan. Every exact-SHA deployment still requires a fresh complete backup.
- Responsive-record ingestion, requester-facing downloads, and redaction are
  implemented as separate integrity-checked Phase 2 surfaces. They still
  require deployment-specific threat-model and acceptance review before
  production exposure. The intake-attachment path is a narrower,
  quarantine-only surface and must not be reused as a RecordItem ingest path.
- Production inbound correspondence requires private object storage, reachable
  ClamAV, Celery workers, and beat. Every dependency fails closed: raw-storage
  failure rejects acceptance while leaving a durable pre-I/O reservation,
  parser/scan failure retains quarantine, and a broker outage leaves durable
  pending work for the recovery sweeps. The reservation stale window must be
  configured deliberately, and operators must acceptance-test process death
  before domain commit, missing broker dispatch, reference healing, and
  storage-deletion failure before enabling inbound polling.
  The object-store identity must be able to put one exact object, get
  bucket-versioning state, list exact-key object versions, head objects, delete
  objects, and delete specific object versions under generated quarantine
  prefixes. The mandatory S3 permissions are `s3:PutObject`,
  `s3:GetBucketVersioning`, `s3:ListBucketVersions`, `s3:GetObject`,
  `s3:DeleteObject`, and `s3:DeleteObjectVersion`. The application never calls
  multipart-upload APIs, so it cannot leave an incomplete multipart upload and
  needs no multipart-abort cleanup path. Versioned quarantine sources and
  delete markers are physically purged and verified before cleanup completes.
  Adopted raw RFC822 is retained legal evidence and must never be
  lifecycle-expired. For raw messages, only application-verified unadopted
  reservation generations are purgeable. Adopted and unadopted raw objects
  use the shared `quarantine/correspondence/raw/` prefix, so prefix-wide object
  lock cannot distinguish adopted evidence from unadopted generations and
  would block required orphan cleanup. Never add a lifecycle expiration to
  that prefix. Govern raw-message retention from the durable receipt/adoption
  evidence; only the application's reference-checked cleanup may delete an
  unadopted raw generation. Attachment quarantine must likewise remain
  purgeable until its durable cleanup completes. Vault-export messages follow
  the same fail-closed boundary: each RFC822 message is written under
  `quarantine/vault-messages/`, verified and classified by ClamAV, and only a
  clean message is promoted into immutable `vault-messages/` evidence and the
  review queue. An infected or unclassified message stays quarantined and the
  ingest run fails without conversion or review exposure. Chat messages take
  that same message path. Drive files from a Vault Drive export are written
  under the same `quarantine/vault-messages/` prefix, scanned on the exact
  bytes, and only a clean file is copied to an immutable `records/` key and
  becomes a `RecordItem`; the quarantine copy is deleted either way, and an
  infected or oversized file is recorded as a failed artifact while the rest
  of the export proceeds. A count-only Drive or Chat export (rule 11) is never
  downloaded. Files a Records
  Officer uploads onto a case (`POST /api/staff/requests/{number}/record-uploads/`)
  and migrated NextRequest files share one path
  (`apps/records/scanned_records.py`): generated keys under
  `quarantine/record-uploads/` or `quarantine/legacy-files/`, a ClamAV verdict
  on the exact bytes, an immutable `records/` copy only when clean, and the
  quarantine object deleted either way. An infected or unscannable upload is
  refused and nothing outside quarantine is written. The case row lock is not
  held across scanning and storage, so a copy retained under `records/` can
  lose a narrow race (a concurrent identical upload, a closure, or a revoked
  role) before its row is written. The runtime role cannot delete under
  `records/`, so that copy stays unreferenced and a
  `records.record_upload_orphaned` audit entry names its key. A future object-lock
  requirement for adopted raw evidence needs a separate retained namespace or
  promotion design instead of a blanket policy on the shared prefix.
- A future requester portal must use an opaque, expiring, single-purpose
  authorization mechanism with throttling and bot protection. A request number
  alone is not authorization.

Re-run this review whenever an endpoint, external call, authentication mode,
upload/download path, reverse-proxy topology, or background integration is
added.
