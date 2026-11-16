# AWS production deployment

Production is `prr.psd401.net`. Application releases use the existing
`psd401-prr-prod` CDK stack and no other deployment mechanism.

## Deploy

From a checkout of merged `main`, run:

```bash
cd infra/cdk && bunx cdk deploy psd401-prr-prod --require-approval never --profile psd401-prr-prod
```

That is the complete production deployment procedure. Nothing runs between
merge and this command.

CDK packages the repository as an immutable application asset, sends it to the
existing production host, builds the application image, creates an application
backup, applies Django migrations, replaces the web, worker, beat, and Caddy
containers, and retains the previous application image for automatic rollback
when replacement fails. These are implementation steps inside the CDK
deployment, not external gates or operator decisions.

The release transaction also preserves an already-approved inbound Gmail
activation only from the exact private activation overlay. It validates the
existing web, worker, and beat as one consistent runtime, first converts stored
eligible unmatched receipts for the exact Gmail mailbox and inbound identity,
then replays the durable Gmail checkpoint. It reports only aggregate reconciled
and remaining counts and requires a later minute-scheduled worker poll to
advance that checkpoint before deployment succeeds. A failed or missing
post-deploy poll rolls the containers back instead of leaving intake silently
disabled. The same transaction performs a non-submitting HTTPS check of
`/request`, requires both signed-form bootstrap cookies, and fetches the built
JavaScript asset. This proves the public form bootstrap is served; it does not
submit to the API or create a synthetic legal request.

CloudWatch alarm
`psd401-prr-prod-gmail-inbound-missing-or-failed` treats either two consecutive
failed health values or two minutes of missing poll telemetry as a breach.
Every stack alarm emails `techalerts@psd401.net` through the
`psd401-prr-prod-alarms` SNS topic when it fires and when it clears. AWS sends a
subscription confirmation email after the first deploy that creates the topic;
nothing is delivered until the recipient confirms it. Recipients are
`alarmEmails` in `infra/cdk/lib/config.ts`.

## District settings are not a deployment

Everything a district can legitimately change is managed in the application,
at **Configuration > District settings**. Only an Application Admin may change
one, every change needs a written reason, and the change takes effect within
seconds across web, worker, and beat without a redeploy. Each setting shows
its current value, what changing it means for a requester or for staff, and
the full history of who changed it, when, and why.

That now covers three kinds of setting:

* the on/off switches - outbound correspondence, the day-5 acknowledgment
  safety net, Gmail transport and inbound polling, staff alerts, advisory AI,
  CloudWatch metrics;
* every limit, rate, timeout, domain, address list, and the advisory model;
* the Google service identities the application acts as, staff sign-in
  including the OAuth client and its secret, and the two operational paths
  (the CloudWatch region and the Office converter).

### Credentials and sign-in

**Rotating a Google identity** means changing which service account the
application impersonates. Deployed Gmail and Vault access is keyless - the
container authenticates as itself through workload identity federation and
impersonates a named service account - so there is no private key to rotate,
and a deployed key file is refused outright. Change the identity on the
Settings page with a reason; the application checks it against Google
immediately and marks the row "Unverified" with the reason if it cannot
authenticate. The value is still saved: the one already in place is the one
that does not work, so refusing the save would help nobody.

**The Google sign-in client secret is write-only.** It can be replaced or
cleared from the Settings page and never read back - not by the page, not by
the API, and not by the Application Admin who set it. The row shows only
whether a value is installed, a fingerprint to compare against the value in
hand, and who installed it when. It is stored encrypted under a key the
database does not hold: `PRR_SETTINGS_ENCRYPTION_KEY` from `.env.app` when an
operator sets one, otherwise derived from `DJANGO_SECRET_KEY`. **Rotating
`DJANGO_SECRET_KEY` without an explicit `PRR_SETTINGS_ENCRYPTION_KEY` makes
every stored secret unreadable**; the application treats them as unset and
they must be entered again. Set `PRR_SETTINGS_ENCRYPTION_KEY` (a Fernet key:
32 random bytes, url-safe base64) if that rotation is foreseeable.

**A sign-in change that would leave no Application Admin able to sign in is
refused**, naming what is missing. The rule counts three doors: local
sign-in, Google (a client ID, a client secret, and an administrator whose
address is in the accepted Workspace domain), and the break-glass Django
admin route with an active superuser. To make a change the rule refuses -
pointing the hosted domain at a new district, for instance - open the
break-glass route and password sign-in first, make the change, then close
them again. Break-glass sign-in is unaffected by a broken OAuth client, which
is what it is for.

Nothing about those switches belongs in `.env.app` any more. The first deploy
after they moved imports each currently deployed value once
(`manage.py import_environment_settings`, run by the deployment immediately
after `migrate` and before the services are replaced), attributes it to a system import, and never overwrites a
stored decision on a later deploy - so that deploy changes no behaviour. After
it, an `.env.app` line for one of those keys is ignored, and the deployment no
longer requires any of them to be present.

If a deployed value for a key that has not been imported yet is one the
application would refuse (out of bounds or malformed), the import fails before
writing anything and names each such key, so the deploy fails and rolls back.
Storing the default in its place would have been permanent. Fix the named
value in `.env.app` and redeploy. A key the district has left unset still
inherits from the variable it used to default from (for example, records-officer
alerts from `PRA_OFFICER_EMAIL`, the advisory region from `AWS_DEFAULT_REGION`).

The fixed `psd401-prr-prod-enable-gmail-inbound` Systems Manager document is
retired for the same reason. It existed to flip two runtime switches safely in
an operator-held file; that is now a page in the application with an
accountable name on every change.

The deployment reuses the production host's existing private `.env` and
`.env.app` files. Never copy an example environment file over either one.

### What `.env.app` still holds

Only the bootstrap set. The application has to read these before it can read
a single stored setting, or Django's own middleware reads them as process
settings before any application code runs, so they are a physical limit
rather than a decision:

| Variable | Why it cannot move |
| --- | --- |
| `DATABASE_URL` (and the Compose-injected Postgres credentials) | the settings store *is* the database |
| `REDIS_URL` | the cache the store propagates through |
| `OBJECT_STORE_ENDPOINT`, `OBJECT_STORE_BUCKET`, `OBJECT_STORE_REGION` (and the Compose-injected object-store credentials) | records storage, reached before any request is served |
| `DJANGO_SECRET_KEY` | signs sessions, and derives the settings-secret key |
| `PRR_SETTINGS_ENCRYPTION_KEY` | opens the stored secrets; a key inside them would be circular |
| `PRR_ENVIRONMENT`, `PRR_DEPLOYMENT_MODE` | decide which safety checks run at startup |
| `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_TRUST_X_FORWARDED_PROTO` | evaluated per request by middleware that runs before any view |
| `GOOGLE_APPLICATION_CREDENTIALS` | the container's own keyless identity, which everything else is impersonated *from* |
| `RUN_MIGRATIONS`, `AUTO_SEED` | read by the entrypoint, before Django starts |
| `DJANGO_DEBUG`, `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SECURE_HSTS_SECONDS`, `DJANGO_SESSION_COOKIE_AGE` | read by Django's security and session middleware from process settings; production leaves them unset and takes the secure defaults |

The malware scanner (`CLAMAV_HOST`, `CLAMAV_PORT`,
`CLAMAV_TIMEOUT_SECONDS`), the Alta camera API (`ALTA_API_BASE`,
`ALTA_API_USERNAME`, and the write-only `ALTA_API_PASSWORD`),
`PORTAL_BASE_URL`, `PRA_SUPERVISOR_EMAIL`,
`PRA_OFFICER_EMAIL`, `FACILITIES_EMAIL`, `DISTRICT_HOLIDAYS`,
`DEFAULT_ESTIMATE_BUSINESS_DAYS`, and `STALL_THRESHOLD_DAYS` moved into the
application in the last part of Phase C, and are imported once like every
other key. Saving a scanner address pings the scanner and marks the row
"Unverified" if it does not answer; saving any of the three Alta keys signs
in and reads the device groups once, and records the verdict under
`ALTA_API_BASE`. Aware has no API tokens: the client signs in as a service
account and shares one 24-hour session through the cache, because Aware keeps
only three sessions per account. `ALTA_API_TOKEN` is retired; nothing reads a
leftover line or stored row for it, and `verify_settings_secrets` names such
a row as retired without failing on it. `DEFAULT_FROM_EMAIL` is no longer read at
all: no application mail uses it, and production refuses every generic Django
send.

Anything not in that table is ignored. A line in `.env.app` for a key the
application manages does nothing once the deployment has imported its values.

**Before the first deploy that carries this move**, check the live `.env.app`
for values the application would refuse, because a refused value fails the
import and rolls the deploy back: `PORTAL_BASE_URL` must be a bare HTTPS
origin with no path or trailing slash, `ALTA_API_BASE` must be the tenant's
HTTPS origin with no path or trailing slash (the client adds `/api/v1`), and
`DISTRICT_HOLIDAYS` must be comma-separated `YYYY-MM-DD` dates.

The Google sign-in client secret lives only in the application. Production's
import carried it out of AWS Secrets Manager on 2026-09-24, and the AWS copy
(`psd401-prr/prod/google-oidc-client-secret`) was then deleted through the
stack. Rotate the secret at Configuration > District settings. The runtime
role's only Secrets Manager permission is reading the runtime secret below.

## Runtime secret

`DJANGO_SECRET_KEY` and `PRR_SETTINGS_ENCRYPTION_KEY` move out of `.env.app`
into the AWS Secrets Manager secret `psd401-prr/prod/runtime`
(psd-dev-standards standards/04 rule 15a). Every Django process reads it at
start through the instance role (`config/runtime_secrets.py`) once
`PRR_RUNTIME_SECRET_ID` is set; without that variable nothing changes. A
missing, unreadable, or malformed secret stops the process instead of starting
with a blank key, and a deploy that cannot start rolls back.

**Production's `PRR_SETTINGS_ENCRYPTION_KEY` is blank** (checked 2026-10-13),
so the settings store is encrypted under a key derived from
`DJANGO_SECRET_KEY`. Copy `DJANGO_SECRET_KEY` exactly and store the settings
key as an explicit `""`. Never generate either value: a new one makes every
stored secret unreadable. The stack creates the secret empty and never writes
it, and keeps it if the stack is deleted.

Every deploy also runs `manage.py verify_settings_secrets`, which fails unless
every stored settings secret decrypts under the release's key. It prints
counts and key names, never values.

The Postgres passwords in `/srv/prr/runtime-config/.env` stay on the host by
decision (Hagel, 2026-10-13). They authenticate only on the private Compose
network; anyone who can read that file can already run `docker exec db psql`.

### 1. Seed the secret (operator, once)

Done on 2026-10-14. Repeat only to re-seed, for example after a deliberate key
change.

1. Sign in, then print short-lived credentials. The second command prints four
   `export AWS_...=...` lines; leave them on screen for step 4.

   ```bash
   aws sso login --profile psd401-prr-prod
   aws configure export-credentials --profile psd401-prr-prod --format env
   ```

2. Open a session on the host:

   ```bash
   aws ssm start-session --target i-016ee26e06547aa82 --profile psd401-prr-prod --region us-west-2
   ```

3. In the session, become root and turn off shell history, so the
   credentials you paste next are never written to `/root/.bash_history`:

   ```bash
   sudo -i
   set +o history
   ```

4. Paste the four `export AWS_...` lines from step 1 exactly as printed, then
   run `export AWS_DEFAULT_REGION=us-west-2`.

5. Paste this block. It copies the two keys from the live release's
   `.env.app` into the secret and prints only the new version id; anything
   else means stop:

   ```bash
   web_id="$(docker ps -q --filter label=com.docker.compose.project=app --filter label=com.docker.compose.service=web)"
   live_dir="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$web_id")"
   python3 - "$live_dir/.env.app" <<'PY' | aws secretsmanager put-secret-value --secret-id psd401-prr/prod/runtime --secret-string file:///dev/stdin --query VersionId --output text
   import json, sys
   values = {}
   for line in open(sys.argv[1], encoding="utf-8"):
       name, sep, value = line.rstrip("\n").partition("=")
       if sep and name in ("DJANGO_SECRET_KEY", "PRR_SETTINGS_ENCRYPTION_KEY"):
           if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
               value = value[1:-1]  # Compose strips one pair of quotes
           values[name] = value
   if len(values.get("DJANGO_SECRET_KEY", "")) < 50:
       sys.exit("DJANGO_SECRET_KEY missing or short; nothing written")
   values.setdefault("PRR_SETTINGS_ENCRYPTION_KEY", "")
   sys.stdout.write(json.dumps(values))
   PY
   ```

6. Clean up, then clear your workstation's terminal scrollback (it showed real
   credentials). The `grep` must print `0`; anything else means a credential
   reached root's history file, so remove those lines before you leave:

   ```bash
   unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_CREDENTIAL_EXPIRATION
   grep -c 'AWS_SECRET_ACCESS_KEY=\|AWS_SESSION_TOKEN=' /root/.bash_history || true
   exit
   exit
   ```

Confirm a current version exists without reading it:

```bash
aws secretsmanager describe-secret --secret-id psd401-prr/prod/runtime --query VersionIdsToStages --profile psd401-prr-prod --region us-west-2
```

Keep a break-glass copy of the secret in the district Bitwarden vault, copied
from the AWS console's **Retrieve secret value**. Never paste it into a
terminal, ticket, or chat.

### 2. Turn it on (code change, then deploy)

`PRR_RUNTIME_SECRET_ID: psd401-prr/prod/runtime` is set in the `x-app-runtime`
block of `docker-compose.aws.yml`; the deploy that carries it switches every
Django process over. Before that deploy, this dry run in the live web
container printed `match` on 2026-10-14. It read the secret through the
instance role and compared it with the running keys, printing no value. With
the current code the same command (below) checks that the secret is readable
and well formed, and that the container's environment holds neither key; it
prints `match` only then.

```bash
web_id="$(docker ps -q --filter label=com.docker.compose.project=app --filter label=com.docker.compose.service=web)"
docker exec -e PRR_RUNTIME_SECRET_ID=psd401-prr/prod/runtime "$web_id" python -c 'import os; from config.runtime_secrets import read_runtime_secret; read_runtime_secret(os.environ); print("match")'
```

During that deploy `.env.app` still held the keys, and a process refused to
start if the file and the secret differed, so the successful deploy proved the
copy exact.

### 3. Remove the keys from the host (operator)

Done on 2026-10-14 over SSM: the `DJANGO_SECRET_KEY` line was removed from
77 files (`/srv/prr/runtime-config/.env.app` and every
`/srv/prr/cdk-releases/*/.env.app`; no file had a `PRR_SETTINGS_ENCRYPTION_KEY`
line), keeping each file's mode and owner. After
`prr-compose up -d --no-deps --force-recreate web worker beat`, no container's
configuration held `DJANGO_SECRET_KEY` and `verify_settings_secrets` reported
1 of 1. Copies in the daily EBS snapshots age out after 14 days. The blank
`PRR_GOOGLE_OIDC_CLIENT_SECRET` and `ALTA_API_TOKEN` lines held no value and
were left alone (`ALTA_API_TOKEN` has since been retired and is ignored).

### 4. Refuse an inline copy

With `PRR_RUNTIME_SECRET_ID` set, a process refuses to start if either key is
also set in its environment, equal to the secret or not, naming the key only.
`.env.app.example` no longer carries either line.

**Emergency fallback** (Secrets Manager unreachable, so nothing can start):
remove `PRR_RUNTIME_SECRET_ID` from `docker-compose.aws.yml` in a change and
deploy it, after writing `DJANGO_SECRET_KEY` back into the live `.env.app` from
the secret through a pipe, never through the terminal. Both are needed:
either one alone still refuses to start.

## Post-deploy housekeeping

The 48 GB root volume has filled before, so the deployment ends by reclaiming
what it superseded. Both steps run only after the new containers are verified
healthy and the rollback trap is disarmed, and both are non-fatal: housekeeping
can never undo a deployment that already succeeded.

- **Images.** Every image no longer pinned by `/srv/prr/deployed-release.json`
  (the live release and its rollback target) is removed with a plain
  `docker rmi`, and the build cache is pruned. Not `docker image prune -a`,
  which judges an image only by whether a container references it and so
  deletes the rollback target.
- **Release directories.** Each deploy unpacks a full copy of the source asset
  into `/srv/prr/cdk-releases/<hash>/`, each with its own `.env.app`; 78 had
  accumulated by 2026-10-14. The deploy now keeps the live release
  (`cdk-current`, which `prr-compose` resolves), every release
  `deployed-release.json` names, the release directory of every container --
  running or stopped -- by its `com.docker.compose.project.working_dir` label
  (which the deploy script's own rollback path reads to restore the previous
  runtime) or by any bind mount pointing into a release, and the three most
  recent directories as a margin -- and deletes the rest. Anything that is not
  a release-hash directory, including a half-unpacked `<hash>.stage`, is left
  alone. If the containers cannot be enumerated, or `deployed-release.json` is
  present but unreadable, nothing is deleted at all and the output says
  `release prune skipped:` rather than reporting a count as if the prune had
  run.

Both report into the deployment output: `root filesystem after prune:` and
`release directories after prune:`.

Two caveats worth knowing before an out-of-band rollback. Nothing in a CDK
deploy writes `/srv/prr/deployed-release.json` today -- it belongs to the
superseded legacy layout described in
[`gmail-transport.md`](gmail-transport.md) -- so in practice both prunes read
it if it happens to exist and pin nothing when it does not. And the release
directories kept as a margin are source, not images: the image prune has no
matching margin, so redeploying an older kept release rebuilds its image from
that source rather than swapping to a retained one.

## Running compose on the host

Use `prr-compose`, never `docker compose` directly:

```bash
sudo prr-compose ps
sudo prr-compose up -d --no-deps --force-recreate web worker beat
sudo prr-compose logs --tail 50 beat
```

Whether inbound polling is on is an application setting, not a property of
the Compose invocation. `/srv/prr/.gmail-inbound-activation.compose.yml` is
retained only so this release's one-time import reads the values production
was actually running; it no longer decides anything, and a later release
removes it. `prr-compose` still resolves the current release, the running
image, and any remaining overlay on every invocation, so an out-of-band
recreate cannot change what is running by forgetting a flag.

If `psd401-prr-prod-gmail-inbound-missing-or-failed` is in ALARM, open
**Configuration > District settings** first and read the Gmail transport
switches and their history - that is where "deliberately off" is recorded, and
it distinguishes an intended pause from a fault before anyone touches the
host.

Production is the only environment; the retired `dev-prr.psd401.net` box was
decommissioned on 2026-10-13.
