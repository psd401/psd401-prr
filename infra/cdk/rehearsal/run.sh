#!/bin/bash
# Rehearses the generated CDK deployment script against warm, cold and partial
# production stacks. Runs inside a Linux container so /srv/prr, flock, and the
# real bash 5 / python3 the script targets are all present.
set -uo pipefail
export PATH=/rehearsal/bin:$PATH
DEPLOY=/rehearsal/deploy.sh

release="$(sed -n 's/^release=//p' "$DEPLOY" | head -1)"
version="$(sed -n 's/^version=//p' "$DEPLOY" | head -1)"
old_release="/srv/prr/cdk-releases/$(printf '%064d' 7)"

# The script re-executes itself under the deployment lock, so `bash -x` would
# not survive; BASH_ENV re-arms xtrace in every bash that runs deploy.sh, and the
# trace goes to its own file so stderr assertions only see the script's messages.
cat > /rehearsal/xtrace-env.sh <<'SH'
if [ "$0" = /rehearsal/deploy.sh ]; then
  exec {rehearsal_xtrace_fd}>>/rehearsal/state/xtrace.log
  BASH_XTRACEFD=$rehearsal_xtrace_fd
  set -x
fi
SH

# Derived from the script's own `install` lines, so a newly installed host file
# can never be missing from the staged release again.
installed_release_files="$(grep -o '^install .*"\$release/[^"]*"' "$DEPLOY" | sed 's/.*"\$release\/\([^"]*\)"$/\1/' | sort -u)"
if [ -z "$installed_release_files" ]; then
  echo "rehearsal: found no \`install ... \$release/...\` lines in the deploy script" >&2
  exit 1
fi

pass=0; fail=0
note() { printf '    %s\n' "$*"; }
check() { # description expected actual
  if [ "$2" = "$3" ]; then pass=$((pass+1)); printf '    ok   %s\n' "$1"
  else fail=$((fail+1)); printf '    FAIL %s\n         expected: %s\n         actual:   %s\n' "$1" "$2" "$3"; fi
}

env_app_body() {
  printf 'PRR_GMAIL_ENABLED=false\nPRR_GMAIL_INBOUND_POLL_ENABLED=false\nPRR_ENVIRONMENT=prod\nRUN_MIGRATIONS=true\n'
}

reset_world() { # $1 = evidence layout: cdk-current | runtime-config | none
  rm -rf /srv/prr /rehearsal/state
  mkdir -p /srv/prr/runtime-config /srv/prr/cdk-releases
  mkdir -p "$release/docker" "$release/infra/aws"
  : > "$release/docker-compose.deploy.yml"
  : > "$release/docker-compose.aws.yml"
  : > "$release/docker/entrypoint.sh"
  # Every host file the script installs from the release must exist in it.
  local f
  for f in $installed_release_files; do
    mkdir -p "$release/$(dirname "$f")"
    printf '#!/bin/bash\nexit 0\n' > "$release/$f"
  done
  printf 'PRR_HOSTNAME=prr.psd401.net\n' > /srv/prr/runtime-config/.env
  mkdir -p "$old_release"
  : > "$old_release/docker-compose.deploy.yml"
  : > "$old_release/docker-compose.aws.yml"
  case "$1" in
    cdk-current)
      env_app_body > "$old_release/.env.app"; chmod 600 "$old_release/.env.app"
      ln -sfn "$old_release" /srv/prr/cdk-current ;;
    runtime-config)
      env_app_body > /srv/prr/runtime-config/.env.app; chmod 600 /srv/prr/runtime-config/.env.app ;;
    none) ;;
  esac
  mkdir -p /rehearsal/state/containers /rehearsal/state/service
  echo "sha256:$(printf '%064d' 1)" > /rehearsal/state/replacement_image_id
  export REHEARSAL_STATE=/rehearsal/state
}

seed_running() { # service image working_dir config_files [mounts,...]
  local id; id="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-12)"
  { echo "service=$1"; echo "image=$2"; echo "working_dir=$3"; echo "config_files=$4"; echo "health=healthy"
    echo "mounts=${5:-}"; } \
    > /rehearsal/state/containers/$id
  echo "$id" > /rehearsal/state/service/$1
}

seed_warm_stack() { # mode
  local base="$old_release/docker-compose.deploy.yml,$old_release/docker-compose.aws.yml"
  local cfg="$base"
  [ "$1" = enabled ] && cfg="$base,/srv/prr/.gmail-inbound-activation.compose.yml"
  local img="sha256:$(printf '%064d' 2)"
  for svc in web worker beat; do seed_running "$svc" "$img" "$old_release" "$cfg"; done
  seed_running db "infra:db" "$old_release" "$base"
}

write_overlay() {
  printf 'services:\n  web:\n    environment: &gmail-inbound-activation\n      PRR_GMAIL_ENABLED: "true"\n      PRR_GMAIL_INBOUND_POLL_ENABLED: "true"\n      RUN_MIGRATIONS: "false"\n      AUTO_SEED: "false"\n  worker:\n    environment: *gmail-inbound-activation\n  beat:\n    environment: *gmail-inbound-activation\n' \
    > /srv/prr/.gmail-inbound-activation.compose.yml
  chmod 600 /srv/prr/.gmail-inbound-activation.compose.yml
}

run_deploy() {
  set +e
  : > /rehearsal/state/xtrace.log
  BASH_ENV=/rehearsal/xtrace-env.sh bash "$DEPLOY" > /rehearsal/state/stdout.log 2> /rehearsal/state/stderr.log
  status=$?
  set -e
  return 0
}

svc_field() { sed -n "s/^$2=//p" "/rehearsal/state/containers/$(cat /rehearsal/state/service/$1)" 2>/dev/null; }
# A failed deploy is only diagnosable from its own stderr, so surface the tail.
check_exit_ok() {
  check "exit status" 0 "$status"
  if [ "$status" -ne 0 ]; then
    note "deploy stderr (last 25 lines):"
    tail -n 25 /rehearsal/state/stderr.log | sed 's/^/      | /'
    # The ERR trap's first line is the rollback call; the command before it failed.
    note "deploy xtrace up to the first rollback:"
    awk '/^\+* rollback( |$)/{exit} {print}' /rehearsal/state/xtrace.log | tail -n 8 | sed 's/^/      | /'
  fi
}
backups() { if [ -f /rehearsal/state/backups.log ]; then wc -l < /rehearsal/state/backups.log | tr -d ' '; else echo 0; fi; }

# ---------------------------------------------------------------- scenario 1
echo "[1] warm stack, Gmail disabled -- existing rolling replacement"
reset_world cdk-current; seed_warm_stack disabled; run_deploy
check_exit_ok
check "backup taken exactly once" 1 "$(backups)"
check "web runs the replacement image" "$(cat /rehearsal/state/replacement_image_id)" "$(svc_field web image)"
check "web adopted the new release dir" "$release" "$(svc_field web working_dir)"
check "beat adopted the new release dir" "$release" "$(svc_field beat working_dir)"
check "caddy recreated" "$release" "$(svc_field caddy working_dir)"
check "cdk-current advanced" "$release" "$(readlink /srv/prr/cdk-current)"
check "release .env.app staged" "0" "$(grep -c 'RUN_MIGRATIONS=true' "$release/.env.app")"
check "backup ran before the migrate step" \
  "$(grep -n 'systemctl --wait start' /rehearsal/state/calls.log | head -1 | cut -d: -f1)" \
  "$(awk '/systemctl --wait start/{print NR; exit}' /rehearsal/state/calls.log)"
note "migrate call index: $(awk '/compose .*run --rm/{print NR; exit}' /rehearsal/state/calls.log), backup index: $(awk '/systemctl --wait start/{print NR; exit}' /rehearsal/state/calls.log)"

# ---------------------------------------------------------------- scenario 2
echo "[2] warm stack, Gmail enabled -- activation preserved"
reset_world cdk-current; write_overlay; seed_warm_stack enabled; run_deploy
check_exit_ok
check "web keeps the activation overlay" "1" "$(svc_field web config_files | grep -c activation)"
check "worker keeps the activation overlay" "1" "$(svc_field worker config_files | grep -c activation)"
check "backup taken exactly once" 1 "$(backups)"

# ---------------------------------------------------------------- scenario 3
echo "[3] COLD stack (nothing running), evidence from /srv/prr/cdk-current"
reset_world cdk-current; run_deploy
check_exit_ok
check "backup still taken exactly once" 1 "$(backups)"
check "dependencies started" "$release" "$(svc_field db working_dir)"
check "web runs the replacement image" "$(cat /rehearsal/state/replacement_image_id)" "$(svc_field web image)"
check "worker runs the replacement image" "$(cat /rehearsal/state/replacement_image_id)" "$(svc_field worker image)"
check "beat runs the replacement image" "$(cat /rehearsal/state/replacement_image_id)" "$(svc_field beat image)"
check "cdk-current advanced" "$release" "$(readlink /srv/prr/cdk-current)"
check ".env.app recovered from durable evidence" "1" "$(grep -c 'PRR_ENVIRONMENT=prod' "$release/.env.app")"
check "no rollback attempted" "" "$(grep 'cold_start_rollback_unavailable' /rehearsal/state/stderr.log)"
note "backup index: $(awk '/systemctl --wait start/{print NR; exit}' /rehearsal/state/calls.log), migrate index: $(awk '/compose .*run --rm/{print NR; exit}' /rehearsal/state/calls.log)"
b=$(awk '/systemctl --wait start/{print NR; exit}' /rehearsal/state/calls.log)
m=$(awk '/compose .*run --rm/{print NR; exit}' /rehearsal/state/calls.log)
check "backup precedes the migration" "yes" "$([ "$b" -lt "$m" ] && echo yes || echo no)"

# ---------------------------------------------------------------- scenario 4
echo "[4] COLD stack, Gmail enabled -- disabled during backup, activated at the end"
reset_world cdk-current; write_overlay; run_deploy
check_exit_ok
check "web ends activated" "1" "$(svc_field web config_files | grep -c activation)"
check "worker ends activated" "1" "$(svc_field worker config_files | grep -c activation)"
check "beat ends activated" "1" "$(svc_field beat config_files | grep -c activation)"
first_worker_up="$(grep -n 'compose .*up .*worker' /rehearsal/state/calls.log | head -1)"
check "worker first started without the activation overlay" "" "$(echo "$first_worker_up" | grep activation)"

# ---------------------------------------------------------------- scenario 5
echo "[5] COLD stack, evidence only in /srv/prr/runtime-config"
reset_world runtime-config; run_deploy
check_exit_ok
check ".env.app recovered" "1" "$(grep -c 'PRR_ENVIRONMENT=prod' "$release/.env.app")"

# ---------------------------------------------------------------- scenario 6
echo "[6] COLD stack with no durable runtime evidence -- must fail loudly"
reset_world none; run_deploy
check "exit status is non-zero" "yes" "$([ "$status" -ne 0 ] && echo yes || echo no)"
check "names the missing evidence" "1" "$(grep -c 'cold start runtime configuration is unavailable' /rehearsal/state/stderr.log)"
check "cdk-current not created" "no" "$([ -e /srv/prr/cdk-current ] && echo yes || echo no)"
check "release left staged" "yes" "$([ -d "$release" ] && echo yes || echo no)"
check "no containers started" "0" "$(ls /rehearsal/state/service | wc -l | tr -d ' ')"

# ---------------------------------------------------------------- scenario 7
echo "[7] PARTIAL stack (web only) -- must stay a loud failure"
reset_world cdk-current
seed_running web "sha256:$(printf '%064d' 2)" "$old_release" "$old_release/docker-compose.deploy.yml,$old_release/docker-compose.aws.yml"
run_deploy
check "exit status is non-zero" "yes" "$([ "$status" -ne 0 ] && echo yes || echo no)"
check "diagnoses the partial runtime" "1" "$(grep -c 'application_partial_runtime_detected' /rehearsal/state/stderr.log)"
check "no backup taken" 0 "$(backups)"

restore_attempted() { grep -c "compose --project-directory $old_release .* up " /rehearsal/state/calls.log; }

# ---------------------------------------------------------------- scenario 8
echo "[8] COLD, failure inside a compose function -- trap fires, no restore"
reset_world cdk-current; REHEARSAL_MIGRATE_FAILS=1 run_deploy
check "exit status is non-zero" "yes" "$([ "$status" -ne 0 ] && echo yes || echo no)"
check "reports that no rollback exists" "1" "$(grep -c 'application_cold_start_rollback_unavailable' /rehearsal/state/stderr.log)"
check "no previous runtime invented" 0 "$(restore_attempted)"
check "cdk-current not advanced" "no" "$([ "$(readlink /srv/prr/cdk-current)" = "$release" ] && echo yes || echo no)"
check "release left staged" "yes" "$([ -d "$release" ] && echo yes || echo no)"

# --------------------------------------------------------------- scenario 8b
echo "[8b] COLD, top-level failure after mutation -- reports that no rollback exists"
reset_world cdk-current; write_overlay; REHEARSAL_VERIFY_GMAIL_FAILS=1 run_deploy
check "exit status is non-zero" "yes" "$([ "$status" -ne 0 ] && echo yes || echo no)"
check "reports that no rollback exists" "1" "$(grep -c 'application_cold_start_rollback_unavailable' /rehearsal/state/stderr.log)"
check "no previous runtime invented" 0 "$(restore_attempted)"
check "cdk-current not advanced" "no" "$([ "$(readlink /srv/prr/cdk-current)" = "$release" ] && echo yes || echo no)"

# ---------------------------------------------------------------- scenario 9
echo "[9] WARM, top-level failure after mutation -- previous runtime restored"
reset_world cdk-current; seed_warm_stack disabled; REHEARSAL_STOP_FAILS=1 run_deploy
check "exit status is non-zero" "yes" "$([ "$status" -ne 0 ] && echo yes || echo no)"
check "restore attempted against the previous release" "yes" "$([ "$(restore_attempted)" -ge 1 ] && echo yes || echo no)"
check "web restored onto the previous release dir" "$old_release" "$(svc_field web working_dir)"
check "web restored to the previous image" "sha256:$(printf '%064d' 2)" "$(svc_field web image)"
check "caddy restored too" "$old_release" "$(svc_field caddy working_dir)"
check "no cold-start message" "0" "$(grep -c 'cold_start_rollback_unavailable' /rehearsal/state/stderr.log)"

# --------------------------------------------------------------- scenario 9b
echo "[9b] WARM, failure inside a compose function -- errtrace reaches the trap"
reset_world cdk-current; seed_warm_stack disabled; REHEARSAL_MIGRATE_FAILS=1 run_deploy
check "exit status is non-zero" "yes" "$([ "$status" -ne 0 ] && echo yes || echo no)"
check "restore attempted against the previous release" "yes" "$([ "$(restore_attempted)" -ge 1 ] && echo yes || echo no)"
check "web restored onto the previous release dir" "$old_release" "$(svc_field web working_dir)"
check "web restored to the previous image" "sha256:$(printf '%064d' 2)" "$(svc_field web image)"
check "no cold-start message" "0" "$(grep -c 'cold_start_rollback_unavailable' /rehearsal/state/stderr.log)"

# --------------------------------------------------------------- scenario 9c
echo "[9c] WARM+Gmail, scheduled-poll retries -- if/|| contexts suppress the trap"
reset_world cdk-current; write_overlay; seed_warm_stack enabled
REHEARSAL_POLL_RETRIES=3 run_deploy
check_exit_ok
check "probe actually retried" "yes" "$([ "$(cat /rehearsal/state/poll_calls)" -ge 4 ] && echo yes || echo no)"
check "no rollback triggered by an expected retry" 0 "$(restore_attempted)"
check "deployment completed onto the new release" "$release" "$(svc_field web working_dir)"
check "cdk-current advanced" "$release" "$(readlink /srv/prr/cdk-current)"

# --------------------------------------------------------------- scenario 9d
echo "[9d] WARM, failing command substitution -- rollback must run exactly once"
# errtrace inherits the ERR trap into command substitutions and into each nested
# function level, so an unguarded rollback here fires three times.
reset_world cdk-current; seed_warm_stack disabled; REHEARSAL_PS_FAILS=1 run_deploy
check "exit status is non-zero" "yes" "$([ "$status" -ne 0 ] && echo yes || echo no)"
check "rollback ran exactly once" 1 "$(restore_attempted)"
check "web restored onto the previous release dir" "$old_release" "$(svc_field web working_dir)"

# ---------------------------------------------------------------- scenario 10
echo "[10] COLD stack, backup gate unavailable -- deployment refuses to migrate"
reset_world cdk-current; REHEARSAL_BACKUP_FAILS=1 run_deploy
check "exit status is non-zero" "yes" "$([ "$status" -ne 0 ] && echo yes || echo no)"
check "reports the backup gate" "1" "$(grep -c 'application_backup_unavailable' /rehearsal/state/stderr.log)"
check "migration never ran" "0" "$(grep -c 'compose .*run --rm' /rehearsal/state/calls.log)"

# --------------------------------------------------------------- scenario 11
# Each release directory is a full copy of the source asset carrying its own
# .env.app; 78 of them had accumulated on the 48G root volume. Which ones the
# prune keeps is the whole safety property, so it is exercised against a real
# /srv/prr rather than asserted on the script's text.
rel_dir() { printf '/srv/prr/cdk-releases/%064d' "$1"; }
exists() { [ -d "$1" ] && echo yes || echo no; }

seed_release_dirs() {
  local i dir
  # Oldest first, each a day apart, so "the 3 most recent" is deterministic.
  for i in $(seq 11 18); do
    dir="$(rel_dir "$i")"
    mkdir -p "$dir"
    env_app_body > "$dir/.env.app"; chmod 600 "$dir/.env.app"
    touch -d "2020-01-$(printf '%02d' $((i - 10)))" "$dir"
  done
  # The previous release, newer than all of those.
  touch -d "2021-01-01" "$old_release"
  # Not release-hash directories: a half-unpacked stage and an operator's own
  # scratch directory must both be left exactly where they are.
  mkdir -p "${release}.stage" /srv/prr/cdk-releases/scratch
  # The deployment record pins the rollback target by path.
  printf '{"release":"%s","image_id":"sha256:%064d","rollback":{"release":"%s","image_id":"sha256:%064d"}}\n' \
    "$release" 1 "$(rel_dir 11)" 2 > /srv/prr/deployed-release.json
}

echo "[11] WARM stack -- prune keeps the live, pinned, running and recent releases"
reset_world cdk-current; seed_warm_stack disabled; seed_release_dirs
# A container still running out of an old release directory: its
# com.docker.compose.project.working_dir label is the only thing pinning it.
seed_running legacy-sidecar "infra:legacy" "$(rel_dir 12)" "none"
# A container an operator started by hand against an old release to look at it:
# no compose label pointing there, only a bind mount into the release.
seed_running inspector "infra:legacy" "/srv/prr" "none" \
  "$(rel_dir 14)/docker-compose.deploy.yml"
run_deploy
check_exit_ok
check "the live release survives" "yes" "$(exists "$release")"
check "cdk-current still resolves" "$release" "$(readlink -f /srv/prr/cdk-current)"
check "the rollback release the record pins survives" "yes" "$(exists "$(rel_dir 11)")"
check "a running container's release directory survives" "yes" "$(exists "$(rel_dir 12)")"
# Only the mount pins this one, so it also proves the format docker is asked
# for actually parses: a format the real daemon rejects prunes nothing at all.
check "a bind-mounted release directory survives" "yes" "$(exists "$(rel_dir 14)")"
# Nothing else pins dir18, so only the keep-recent margin can save it. The
# previous release is pinned by the db container that keeps running out of it,
# so it proves the label pin rather than the margin.
check "the most recent otherwise-unpinned release survives (margin)" "yes" "$(exists "$(rel_dir 18)")"
check "the previous release survives (its db container still runs there)" "yes" "$(exists "$old_release")"
# Every directory that nothing pins, named individually: an off-by-one in the
# margin or a short-circuited loop cannot hide behind a count.
for pruned in 13 15 16 17; do
  check "unpinned release $pruned is pruned" "no" "$(exists "$(rel_dir "$pruned")")"
done
check "a half-unpacked stage directory is left alone" "yes" "$(exists "${release}.stage")"
check "a non-release directory is left alone" "yes" "$(exists /srv/prr/cdk-releases/scratch)"
# Exactly the six release directories named above, plus the stage and scratch
# directories the prune must not touch.
check "reported count" "8" \
  "$(sed -n 's/^release directories after prune: //p' /rehearsal/state/stdout.log)"
check "no skip reported" "0" \
  "$(grep -c 'release prune skipped' /rehearsal/state/stdout.log)"
check "prr-compose still resolves the live release" "0" \
  "$(PRR_COMPOSE_QUIET=1 /usr/local/sbin/prr-compose ps web >/dev/null 2>&1; echo $?)"

# -------------------------------------------------------------- scenario 11b
echo "[11b] WARM stack -- prune cannot read the running containers, so it prunes nothing"
reset_world cdk-current; seed_warm_stack disabled; seed_release_dirs
REHEARSAL_PS_ALL_FAILS=1 run_deploy
check_exit_ok
check "nothing pruned when enumeration fails" "yes" "$(exists "$(rel_dir 13)")"
check "the live release survives" "yes" "$(exists "$release")"
check "deployment still succeeded" "$release" "$(svc_field web working_dir)"
# A prune that quietly stops running is how the disk fills again, so the skip
# has to be in the deployment output rather than inferred from a count.
check "the skip is reported" "1" \
  "$(grep -c 'release prune skipped' /rehearsal/state/stdout.log)"

# -------------------------------------------------------------- scenario 11c
echo "[11c] WARM stack -- an unreadable release record prunes nothing"
reset_world cdk-current; seed_warm_stack disabled; seed_release_dirs
# Present but corrupt: it still pins the rollback target, and nothing here can
# tell which one, so refusing is the only safe answer.
printf '{"rollback": \n' > /srv/prr/deployed-release.json
run_deploy
check_exit_ok
check "nothing pruned when the record cannot be read" "yes" "$(exists "$(rel_dir 13)")"
check "the live release survives" "yes" "$(exists "$release")"
check "the skip is reported" "1" \
  "$(grep -c 'release prune skipped' /rehearsal/state/stdout.log)"

echo
echo "rehearsal: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
