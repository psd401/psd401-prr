# ADR 0001: Direct Google Workspace identity

- Status: Accepted; deployment topology superseded by ADR 0002
- Date: 2026-07-22

ADR 0002 superseded this decision's deployed-development topology on
2026-08-18. The direct Google identity choice and security properties remain;
production is now the sole deployment target, and dev is a legacy inventory
candidate pending separately approved decommissioning.

## Decision

Use Direct Google Workspace OIDC for deployed staff authentication. Keep
authorization in this application: administrators map existing, arbitrarily
named Workspace groups to the stable Records Officer, Department Liaison,
Intake Clerk, Reviewer, and Application Admin roles.

Do not put Amazon Cognito between the application and Google. The district has
one identity provider, all domain accounts already use Google two-factor
authentication, and Cognito would add a second identity and session control
plane without adding a current capability.

Developers use explicit local identity mode and the same application roles.
They do not need Google Cloud, Google Admin, AWS, production OAuth, or
production secret access. Password login routes are absent from Google mode;
an incident-only superuser break-glass backend can be enabled independently.

## Security consequences

- The application validates Google signature, nonce, issuer, audience,
  authorized presenter, verified email, exact hosted domain, and immutable
  `sub` before provisioning a passwordless user.
- Login asks only for `openid email profile`. A separate credential with the
  Cloud Identity read-only scope and the Workspace Groups Reader role checks
  one member against only the configured groups; application code never lists
  a group's members and never grants nested membership.
- Groups Reader is assigned once to the keyless service account so application
  administrators can map another existing group without a Google Admin change.
  The accepted tradeoff is directory-wide read access if that runtime identity
  is compromised; exact AWS workload federation, no private key, read-only
  scope, configured-group-only code paths, and fail-closed sessions contain
  that risk. It never receives Groups Editor, Groups Administrator, or
  domain-wide delegation.
- Role membership is refreshed during active sessions. Removal expires the
  session; a sufficiently stale unavailable directory fails closed.
- Email is never the identity key, and an existing local account requires a
  one-use administrator approval before its Google subject can be attached.
- Role mapping changes are audited and invalidate Google sessions. Legal
  human-only operations remain human-only regardless of role.

## Infrastructure consequences

The single Terraform root owns one shared Google project, required APIs,
dedicated group-reader service account, and empty secret container. It outputs
the exact production callback and a legacy dev inventory candidate; Terraform
cannot inspect the manually managed OAuth client to prove either registration.
Terraform never creates a service-account private key or stores an OAuth secret
value in state. Production uses its exact AWS runtime role and restricted
Workload Identity Federation provider. The retained dev provider configuration
exists only until the separate inventory/decommission removes it. One
documented Google Admin/Auth Platform task creates the client and grants the
reader the one-time Workspace Groups Reader role.

There is no GitHub Actions deployment identity yet because deployment is not
being automated and not every developer has AWS access. CI continues to test
the code and Terraform statically without receiving production credentials.

Reconsider Cognito only if a real requirement appears for multiple identity
providers, AWS-native user lifecycle federation, or a shared authentication
control plane across several applications.
