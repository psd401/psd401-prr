# Deployment rehearsal

`bun run rehearse` (from `infra/cdk`) executes the **synthesized** application
deployment script — the exact shell CDK sends to Systems Manager — against a
warm, a cold, and a partial production stack. It needs a running Docker daemon.

```bash
cd infra/cdk && bun run rehearse
```

## Why this exists

`directDeploymentScript` is the only path to production, and most of it cannot
be reached from `bun test`: the unit tests assert on the script's text, not on
what it does. The behaviour that matters most — what happens when production is
already down — has no other way to be exercised before a real deploy.

The harness runs the script in `ubuntu:24.04` because the script targets Ubuntu:
bash 5 array expansion, `flock`, a real `/srv/prr`, and an `ubuntu` account all
have to exist for a rehearsal to mean anything. macOS bash 3.2 cannot run it.

## What is stubbed, and what is not

The deployment script itself is **not** stubbed or modified. `bin/` holds
minimal stand-ins for the host commands it calls:

| Stub | Models |
| --- | --- |
| `docker` | container/service state, compose `up`/`ps`/`run`, `inspect`, `exec`, `stop`, rollback `tag`, the post-deploy prune (`images`/`rmi`/`builder`), and the in-app settings store's Gmail mode (it agrees with the container's activation overlay) |
| `systemctl` | `daemon-reload`/`enable` succeed; the pre-deploy backup gate — enforces `run-app-backup.sh`'s real precondition that exactly one `web`, `db`, `worker` and `beat` container is running |
| `curl` | a valid public-intake bootstrap page and cookie jar (the script's own Python validators still parse them for real) |
| `aws` | fails loudly; the release is pre-staged, so it should never be called |
| `sleep` | no-op, so retry loops finish quickly |

The staged release carries every host file the script `install`s from the
release. `run.sh` reads that list from the script's own `install` lines, so a
newly installed file needs no harness change.

When a scenario expected to succeed fails, the log prints the tail of the
script's stderr and the last xtrace lines before the ERR trap fired, so the
failing command is named. The script re-executes itself under the deployment
lock, so `run.sh` re-arms xtrace through `BASH_ENV` and writes it to a separate
file; stderr assertions only ever see the script's own messages.

Failure injection is by environment variable: `REHEARSAL_MIGRATE_FAILS`,
`REHEARSAL_BACKUP_FAILS`, `REHEARSAL_STOP_FAILS`,
`REHEARSAL_VERIFY_GMAIL_FAILS`.

## Scenarios

1. Warm stack, Gmail disabled — the ordinary rolling replacement.
2. Warm stack, Gmail enabled — activation overlay preserved across the replacement.
3. Cold stack, `.env.app` recovered from `/srv/prr/cdk-current`.
4. Cold stack, Gmail enabled — brought up disabled for the backup, activated at the end.
5. Cold stack, `.env.app` recovered from `/srv/prr/runtime-config`.
6. Cold stack with no durable runtime evidence — fails by name, release left staged.
7. Partial stack (web only) — stays a loud failure.
8. Cold, failure inside a compose function — no phantom rollback, release staged.
   8b. Cold, top-level failure after mutation — reports that no rollback exists.
9. Warm, top-level failure after mutation — previous runtime restored.
   9b. Warm, failure inside a compose function — see below.
10. Cold, backup gate unavailable — refuses to migrate.

## The ERR trap, errtrace, and the subshell guard

The script sets `set -Eeuo pipefail`. The `-E` (`errtrace`) is load-bearing:
without it the `trap rollback ERR` is **not inherited by shell functions**, and
every mutating compose command runs through `compose_runtime` / `compose_mode` /
`compose_base`, which are functions. A failed container replacement would exit
without ever rolling back. Only top-level command failures reached the trap.

`errtrace` also propagates the trap into command substitutions and into each
nested function level inside them, so `rollback` carries a guard:

```bash
if test "$BASHPID" != "$$"; then exit "$code"; fi
```

Measured on bash 5.2, for `web_id="$(compose_runtime ps -q web)"`:

| Context | ERR trap fires |
| --- | --- |
| `x="$(failing)"` | twice — once in the subshell, once in the parent |
| `x="$(outer)"` where `outer` calls another function | three times |
| `if x="$(failing)"; then` | not at all |
| `x="$(failing)" \|\| true` | not at all |

Without the guard a single failed probe re-creates the previous runtime two or
three times. Scenario 9d pins this to exactly one restore; scenario 9c pins that
the `if`-guarded scheduled-poll retry loop, which returns non-zero on every
attempt until beat enqueues its task, triggers no rollback at all.
