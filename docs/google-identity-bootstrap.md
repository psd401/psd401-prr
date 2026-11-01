# Google staff identity bootstrap

This is the human-run Google bootstrap for production. Terraform creates
everything Google exposes safely as
infrastructure. Google does not currently offer a supported Terraform resource
for an ordinary Google Auth Platform web client, the Workspace Groups Reader
assignment, or the Workspace domain-wide delegation grant, so those remain the
documented console steps.
Never place the client secret in Terraform, a tfvars file, Terraform state,
source control, a developer `.env`, or a GitHub Actions secret.

## Production identity

| Environment | Origin | OAuth callback |
|---|---|---|
| Production | `https://prr.psd401.net` | `https://prr.psd401.net/api/staff/auth/oidc/callback/` |

Production is the only environment. Terraform outputs the production callback
but cannot inspect the manually managed Google Auth Platform client. The retired
`dev-prr.psd401.net` site was decommissioned on 2026-10-13: if its origin or
callback is still registered on the client, delete it, and never add it back.

Never copy production application data or runtime secrets into a local test
environment. Developers do not need Google Cloud, Google
Admin, AWS, production OAuth, or production secret access.
Local mode continues to use ordinary local Django users and stable application
roles.

## Before starting

- Apply `infra/terraform/google-identity` with Terraform as a district Google Cloud
  organization operator and confirm a zero-change follow-up plan.
- Run `AWS_PROFILE=psd401-prr-prod terraform output oidc_callback_urls`; register the
  production value exactly.
- Identify the existing Workspace groups that will map to Records Officer,
  Intake Clerk, Department Liaison, Reviewer, and Application Admin. The application assumes
  no naming prefix and supports more than one group per role.

## One Google operator task

1. Open **Google Auth Platform** in `psd401-prr-identity`.
2. Configure the application as **Internal** to the PSD401 organization. Use a
   district-owned support address and only the minimum `openid email profile`
   identity scopes.
3. Create one Web application OAuth client with the exact production redirect
   URI from the table above. Do not add wildcard, HTTP, IP-address, frontend-development, or
   localhost redirect URIs.
4. Capture the client ID and the client secret, and enter both at
   **Configuration > District settings** with a written reason. The secret is
   write-only: it is encrypted at rest under a key the database does not
   hold, and nobody — including the administrator who enters it — can read it
   back. Never copy it into `.env.app`. An already-running deployment keeps
   reading its AWS Secrets Manager value until the one-time import writes it
   into the store; after that, rotate it on the Settings page.
5. In **Google Admin → Account → Admin roles → Groups Reader**, choose
   **Assign admin → Assign service accounts** and assign the read-only role to
   `prr-google-group-reader@psd401-prr-identity.iam.gserviceaccount.com`. This
   is a one-time Workspace assignment: do not add the service account to each
   mapped group. Do not grant Groups Editor, Groups Administrator, any other
   admin privilege, or domain-wide delegation. Groups Reader can read
   directory-wide group membership, while the application deliberately checks
   only the named person's direct membership in administrator-configured
   groups and never lists a group's members. Role groups must contain users
   directly; nested membership is intentionally not accepted.
6. Provide production a Google external-account/ADC credential file and set
   its path as the **Google group reader credential file** setting. Terraform
   never creates a private key.
   Keep the production AWS runtime role in the Terraform `aws_runtimes` map.
   Generate the production
   non-secret external-account configuration with
   `gcloud iam workload-identity-pools create-cred-config`, targeting
   `prr-google-group-reader`, and mount it only in the matching runtime. Also
   generate the bare runtime ADC file described below for Gmail and Vault; do
   not reuse the group-reader file as their source credential. A temporary Google key,
   if unavoidable before AWS, belongs only in the operator-controlled secret
   store and must be rotated out.

## Gmail domain-wide delegation

This is a second, deliberately separate identity. Never reuse the Vault or
group-reader service account for Gmail.

1. Copy the numeric client ID from
   `AWS_PROFILE=psd401-prr-prod terraform output -raw gmail_service_account_unique_id`.
   The corresponding service-account email is available from
   `terraform output -raw gmail_service_account`.
2. Open **Google Admin → Security → Access and data control → API controls →
   Domain-wide delegation**, then choose **Add new**.
3. Paste that numeric client ID and grant exactly these two comma-separated
   scopes—no Drive, Directory, Vault, full-mail, or other scope:
   `https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/gmail.modify`.
4. Confirm `publicrecords@psd401.net` is a real licensed Workspace mailbox,
   not a Google Group or group alias. The Gmail API cannot act as a group.
5. Configure one runtime as below and run
   `python manage.py verify_gmail`. It must report the pinned subject, both
   scopes, and the same mailbox profile without sending a message.

The grant is **domain-wide by nature**: Workspace authorizes the service
account to delegate within the domain for those two scopes. Terraform cannot
restrict that delegation to one mailbox. The application supplies only the
pinned `PRR_GMAIL_MAILBOX` as the delegated subject, verifies the returned
profile is the same mailbox, and refuses a deployed private-key credential.
Those application checks are the mailbox boundary; reviewers must evaluate the
Workspace grant and the pinned-subject code together.

For production, generate a second external-account configuration from the
production provider, this time with **no**
`--service-account` option:

```bash
gcloud iam workload-identity-pools create-cred-config \
  projects/<number>/locations/global/workloadIdentityPools/prr-aws-runtime/providers/<provider> \
  --aws --enable-imdsv2 \
  --output-file=prr-google-runtime-credential.json
```

This bare ADC file contains no secret and carries no Google privilege by
itself. The Gmail client uses it to prove the configured AWS runtime role, then
impersonates only `prr-gmail` through Terraform bindings for
`roles/iam.workloadIdentityUser` and the service-account-scoped
`roles/iam.serviceAccountTokenCreator` role required to sign the delegated
Workspace JWT. The latter is never granted on the group reader. Store it beside
the group-reader credential and mount both read-only. Never add a private key
to either file.

## Vault domain-wide delegation

Vault uses a third dedicated service account. Never reuse the Gmail transport
or group-reader identity for eDiscovery.

1. Apply `infra/terraform/google-identity`, then copy the numeric client ID from
   `terraform output -raw vault_service_account_unique_id`.
2. In **Google Admin → Security → Access and data control → API controls →
   Domain-wide delegation**, add that client ID with exactly these scopes:
   `https://www.googleapis.com/auth/ediscovery,https://www.googleapis.com/auth/devstorage.read_only`.
3. Choose a named, licensed Workspace account and enter it as the **Vault
   administrator account** setting. Give that account only the Vault
   privileges required to manage PRR matters, searches, holds, and exports.
   The application delegates as this exact user; it never delegates as the
   signed-in Records Officer. Rotating to a permanent operator is a Settings
   change with a reason on the record, not a host edit or a rebuild; the
   application checks the new identity against Google on save and marks the
   row "Unverified" if it cannot authenticate.
4. Use the same production bare runtime ADC file created above. Terraform
   permits that AWS runtime to impersonate only the dedicated
   `prr-vault` target and sign the delegated JWT. No Google private key is
   created or deployed.

The request page exposes count-only Gmail scope checks to Records Officers.
Google requires a Vault matter for a count, so the first count may create the
empty request matter. It never creates a hold, saved query, export, collection
task, legal outcome, or requester communication. Google does not expose the
same count method for Drive. Each count requires an explicit scope: one or more
named district accounts, or all accounts in the district Workspace. An empty
custodian list never silently widens a search.

## First application configuration

Production has its own application database and needs one initial Application
Admin mapping. The steps below describe the first-turn-up security contract;
they are not an approved standalone restart sequence for the already-running
service.

Both break-glass switches are district settings now, checked on the request
rather than at process start, so this no longer needs a recreate of
web/worker/beat — which also means the escape hatch can be opened during the
incident it exists for rather than only at the boot before it:

1. At **Configuration > District settings**, turn on the Django admin
   break-glass route and then break-glass password sign-in, each with a
   written reason, for a named, vaulted break-glass production superuser.
   (The route has to be on first: the password switch depends on it.)
2. Open `/admin/`, add the initial Google group mappings, then test a Google
   Application Admin login in a separate browser session.
3. Turn both switches back off, in the reverse order, with a reason. The
   password sign-in route answers nothing outside local sign-in mode, and the
   `/admin/` prefix returns 404 while the route is off.
4. Future mapping changes happen at **Configuration → Staff identity**. Every
   change is audited and expires Google staff sessions so roles are checked
   again. The one-time Groups Reader assignment means adding a mapping does not
   require another Google Admin change.

The application permits a Workspace group to be explicitly mapped to multiple
roles in production; it does not enforce group separation. Each role
still requires its own audited mapping row, so Application Admin authority
never implicitly grants Records Officer authority.

During pre-launch build and testing, `tsd-engineering@psd401.net` may be
explicitly mapped to all five roles so the team can test the complete workflow
before permanent role groups exist. Replacing that temporary mapping before
live public-records casework is an operational governance decision, not an
application constraint.

## Runtime values

Use the production hostname:

What is left on the host is what the application must read before it can read
a stored setting, plus the container's own keyless identity:

```text
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=prr.psd401.net
DJANGO_CSRF_TRUSTED_ORIGINS=https://prr.psd401.net
GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/prr-google-runtime-credential.json
PRR_GMAIL_ENABLED=false
```

Everything else here is now a district setting, changed at **Configuration >
District settings** by an Application Admin with a written reason and a full
history:

| Setting | Value to configure |
| --- | --- |
| Staff sign-in mode | `google` |
| Workspace sign-in domain | `psd401.net` |
| Google sign-in client ID | the shared client id |
| Google sign-in client secret | the shared client secret (write-only) |
| Google sign-in request timeout | `10` seconds |
| Google group reader credential file | `/run/secrets/prr-google-groups-credential.json` |
| Gmail service identity | `prr-gmail@psd401-prr-identity.iam.gserviceaccount.com` |
| Vault service identity | `prr-vault@psd401-prr-identity.iam.gserviceaccount.com` |
| Vault administrator account | the named, licensed Workspace account |
| Allowed Workspace group domains | `psd401.net` |
| Group membership recheck / maximum staleness | defaults are the shipped values |
| Gmail mailbox and timeout | defaults are the shipped values |

The defaults are the values above, and an existing deployment's values are
imported once on the first deploy after they moved, so a turn-up needs no
action for any of them. The two key-file settings (Gmail and Vault) exist for
a developer's machine only: a deployed instance refuses a private key and
uses keyless impersonation.

The client secret is write-only. It can be replaced or cleared on the Settings
page and never read back; the row shows only whether one is installed, a
fingerprint, and who installed it when. It is stored encrypted under a key the
database does not hold — see the `.env.app` section of
[aws-deploy.md](aws-deploy.md) for `PRR_SETTINGS_ENCRYPTION_KEY` and what
rotating `DJANGO_SECRET_KEY` without one costs.

A sign-in change that would leave no Application Admin able to sign in is
refused. Open the break-glass admin route and password sign-in first if you
need to make one anyway.

Run `python manage.py check --deploy --fail-level ERROR` before serving
traffic. It no longer judges sign-in: those values live in the settings store,
so the deploy's settings import logs an error on the
`apps.configuration.signin` logger when nobody could sign in, and the
Settings page refuses a change that would cause it. Keep Gmail disabled until the delegation and `verify_gmail` check pass;
direction-specific switches are documented in
[`gmail-transport.md`](gmail-transport.md).

## Recovery and rotation

- A disabled or removed group member loses access at the next 15-minute check;
  the application expires the Django session immediately.
- If Google group verification remains unavailable beyond the 30-minute stale
  window, staff sessions fail closed.
- Rotate the OAuth secret only through the tested, globally locked production
  deployment mechanism in [`aws-deploy.md`](aws-deploy.md). It updates the
  runtime store, recreates production web/worker/beat together, verifies the
  production login flow, and only then removes the old secret. Do not use a
  standalone restart. Handle any not-yet-decommissioned dev credential through
  the separate decommission plan rather than restarting it as a release target.
- Break-glass use must be incident-scoped, audited, disabled immediately after
  use, and tested quarterly in production.

## Google references

- [Google OpenID Connect validation](https://developers.google.com/identity/openid-connect/openid-connect)
- [Cloud Identity authentication and authorization](https://cloud.google.com/identity/docs/concepts/auth)
- [Direct membership lookup API](https://cloud.google.com/identity/docs/reference/rest/v1/groups.memberships/lookup)
- [Assign a Workspace admin role to a service account](https://support.google.com/a/answer/9807615)
- [AWS Workload Identity Federation](https://cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds)
