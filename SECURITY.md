# Security Policy

This repository holds the Public Records Act request management system that
Peninsula School District No. 401 (WA) runs in production at
`prr.psd401.net`. It handles public records requests under **RCW 42.56** and
touches student records governed by **FERPA**, so security reports are taken
seriously and triaged by the repository owner.

## Reporting a vulnerability

**Email the district technology alerts mailbox: `techalerts@psd401.net`.** Put
"security" in the subject line. This channel always works.

If GitHub's private vulnerability reporting is enabled on this repository, you
may use it instead — the **Report a vulnerability** button under the
[Security tab](https://github.com/psd401/psd401-prr/security). It keeps the
report private between you and the district until an advisory is published. If
you do not see that button, the setting is off; use email.

**Do not open a public issue or pull request for a security problem**, and do
not post one in a discussion. A public report is a disclosure.

### Please do not include real data

This system processes personal information about requesters and, in FERPA
matters, about students. A vulnerability report never needs it:

- Describe the flaw and the request that triggers it, not the record it
  returned.
- Redact names, email addresses, student identifiers, and record contents.
- If you believe you accessed real records, say so and stop. Do not download,
  retain, or share them, and do not probe further to measure the blast radius.

A report containing real student or requester data creates the disclosure it
is meant to prevent.

## What to include

- The affected endpoint, file, or workflow.
- The steps to reproduce, and what an attacker gains.
- The commit SHA or deployed version you tested.
- Any proposed fix, if you have one.

## Scope

**In scope** — this repository and the production deployment at
`prr.psd401.net`:

- Authentication and authorization, including staff role resolution and the
  Google group mapping.
- The anonymous public intake API (`POST /api/public/requests/`) and its file
  upload and malware scanning path.
- The staff REST API and both MCP transports.
- Exemption, redaction, and release logic, where a flaw could disclose an
  exempt or FERPA-protected record.
- The append-only audit trail and status event log, where a flaw could let
  them be altered.
- Infrastructure defined under `infra/`.

**Out of scope:**

- Third-party services the district does not operate (Google Workspace, AWS,
  Avigilon Alta), except where this repository misconfigures them. Report
  those to the vendor.
- Denial of service by volume, load testing, or automated scanning against
  production. Do not run these against `prr.psd401.net`.
- Findings that depend on already having a privileged district account.
- Reports from automated tooling with no demonstrated impact.
- Missing hardening headers or other best-practice gaps with no exploit path.
  These are welcome as ordinary issues, not as vulnerability reports.

## Testing rules

Do not test against production. `prr.psd401.net` serves live public records
requests, and a test there is an incident. Run the stack locally instead —
`docs/development.md` has the setup.

Do not access, modify, or delete data belonging to anyone else, and do not
attempt social engineering against district staff.

## Handling

The repository owner acknowledges reports through the reporting channel and
keeps the reporter updated as triage proceeds. Fixes ship through the normal
pull request path, which runs CI, a secret scan, and an automated review.
Where the district judges it useful to others, a GitHub security advisory is
published after the fix is deployed, crediting the reporter unless they ask
otherwise.

This project has no bug bounty and offers no payment.

## Supported versions

Only `main`, as deployed to production, is supported. There are no maintained
release branches and no backports; fixes land on `main` and deploy from there.

| Version | Supported |
| --- | --- |
| `main` (deployed to `prr.psd401.net`) | Yes |
| Any earlier commit or fork | No |
