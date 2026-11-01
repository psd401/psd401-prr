# OpenWiki brief for this repository

OpenWiki reads this file as the brief for every run and never rewrites it
(psd-dev-standards `standards/06-cicd-enforcement.md` §3). `.openwikiignore` at
the repository root is the hard boundary; this file covers what the boundary
cannot, because the source itself carries the detail. These are instructions to
a model, so they reduce the risk rather than guarantee it.

This is a Washington State public-records system (RCW 42.56) that also handles
student records (FERPA). Two rules follow from that.

## Describe people and identities by role, never by name

The codebase, its tests, and its fixtures name real mailboxes, service
accounts, and staff — the delegated Gmail mailbox, the retired records
officer's address, the Vault subject account, the identity-bootstrap group.
Write "the delegated intake mailbox", "the Vault service identity", "the
records officer", "the pre-launch test group". Do not put a staff or student
name, a personal or named mailbox address, a service-account address, a tenant
ID, an AWS account ID, a Google organization or billing account ID, a bucket
name, an ARN, or a credential location into generated prose,
even when a source file, a test, a fixture, or a commit message contains it.

The one exception is the district's public production hostname and the
public-facing records-request address, both of which are already published on
the district's own public form.

## Do not describe how to get around a control

Describe what a safeguard is for, not how to defeat it. Leave out ways around
authentication or authorization, gaps in the compliance guards, and anything
that reads as a route to records a requester is not entitled to. Exemption
logic and deadline math are legal artifacts: describe what they enforce and
cite the rule number, and do not restate a worked example containing real case
data.

## Take commands from the source, not from earlier wiki pages

Copy setup, test, and deploy commands from `AGENTS.md` and
`docs/development.md` as they stand on this run, and only name a file that
exists in the repository. Python dependencies install with `uv sync --locked`
from the committed `uv.lock`; there is no `requirements.txt`, so never write a
`pip install -r requirements.txt` or `uv pip install` step.
