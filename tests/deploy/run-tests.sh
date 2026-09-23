#!/usr/bin/env bash
# Tests for deploy/deploy-lib.sh. Plain bash, no bats needed.
# docker, curl, and ssh are stubs (tests/deploy/stubs); git is real, against
# a throwaway repo with a bare "origin", so tag creation and push are real.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../../deploy/deploy-lib.sh"
PASS=0 FAIL=0 CURRENT=""

fail() { printf '  FAIL [%s]: %s\n' "$CURRENT" "$*"; FAIL=$((FAIL + 1)); }
ok()   { PASS=$((PASS + 1)); }
assert_eq()       { [[ "$1" == "$2" ]] && ok || fail "expected '$2', got '$1' ${3:-}"; }
assert_contains() { [[ "$1" == *"$2"* ]] && ok || fail "expected to find '$2' in: ${1:0:600}"; }
assert_not_contains() { [[ "$1" != *"$2"* ]] && ok || fail "did not expect '$2' in: ${1:0:600}"; }

# Fresh sandbox: state dir, compose dir, git repo with a bare origin.
setup() {
  CURRENT="$1"
  T=$(mktemp -d)
  export STUB_STATE="$T/state"; mkdir -p "$STUB_STATE"/{images,ps,inspect,tags}
  : >"$STUB_STATE/calls"
  mkdir -p "$T/compose"
  git init -q --bare "$T/origin.git"
  git init -q -b main "$T/repo"
  git -C "$T/repo" config user.email t@example.com
  git -C "$T/repo" config user.name t
  git -C "$T/repo" config core.hooksPath /dev/null
  echo one >"$T/repo/a.txt"
  git -C "$T/repo" add a.txt
  git -C "$T/repo" commit -q -m one
  git -C "$T/repo" remote add origin "$T/origin.git"
  SHA=$(git -C "$T/repo" rev-parse HEAD); SHORT=${SHA:0:12}
  # One service "web", image "hub-web" (no tag), running container c1 on image OLDID.
  echo web >"$STUB_STATE/services"
  echo hub-web >"$STUB_STATE/images/web"
  echo c1 >"$STUB_STATE/ps/web"
  echo sha256:OLDID >"$STUB_STATE/inspect/c1"
  echo 'ok {"ok":true}' >"$STUB_STATE/health"
}

# Run dl_run in a subshell with stubs first on PATH. Sets RC and OUT.
run_deploy() {
  OUT=$({
    PATH="$HERE/stubs:$PATH"
    # shellcheck source=../../deploy/deploy-lib.sh
    source "$LIB"
    DL_SERVICE=hub DL_COMPOSE_DIR="$T/compose" DL_HEALTH_URL="http://box/health"
    DL_HEALTH_JQ='.ok == true' DL_HEALTH_TIMEOUT=1 DL_HEALTH_INTERVAL=0.05 DL_GIT_DIR="$T/repo"
    for kv in "$@"; do eval "$kv"; done
    dl_run
    echo "RC=$?"
  } 2>&1)
  RC=$(printf '%s\n' "$OUT" | sed -n 's/^RC=//p' | tail -n 1)
  CALLS=$(cat "$STUB_STATE/calls")
}

teardown() { rm -rf "$T"; }

# ---------------------------------------------------------------- tests

setup "healthy deploy records pre tag, deploys, tags git"
run_deploy
assert_eq "$RC" 0
assert_eq "$(cat "$STUB_STATE/tags/hub-web_pre-$SHORT")" "sha256:OLDID" "(pre tag points at old image)"
assert_contains "$CALLS" "docker compose up -d --build"
assert_contains "$CALLS" "curl -fsS --max-time 10 http://box/health"
tags=$(git -C "$T/origin.git" tag -l 'deploy/hub/*')
[[ "$tags" =~ ^deploy/hub/[0-9]{8}T[0-9]{6}Z-$SHORT$ ]] && ok || fail "pushed tag looks wrong: '$tags'"
assert_eq "$(git -C "$T/origin.git" rev-parse "$tags^{commit}")" "$SHA" "(tag points at deployed commit)"
assert_contains "$OUT" "docker tag hub-web:pre-$SHORT hub-web:latest"
assert_contains "$OUT" "up -d --no-build --force-recreate web"
teardown

setup "unhealthy deploy rolls back and reports 1"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 1
assert_contains "$CALLS" "docker tag hub-web:pre-$SHORT hub-web:latest"
assert_contains "$CALLS" "docker compose up -d --no-build --force-recreate web"
assert_eq "$(cat "$STUB_STATE/tags/hub-web_latest")" "sha256:OLDID" "(latest retagged to old image)"
assert_eq "$(git -C "$T/origin.git" tag -l 'deploy/*')" "" "(no deploy tag on failure)"
assert_contains "$OUT" "rolled back and the old version is healthy"
teardown

setup "predicate false counts as unhealthy"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2
assert_contains "$OUT" "predicate .ok == true was not true"
assert_contains "$OUT" "ROLLBACK FAILED"
teardown

setup "health that recovers within the timeout passes"
printf 'fail\nok {"ok":false}\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=5
assert_eq "$RC" 0
assert_contains "$OUT" "healthy after 3 check(s)"
teardown

setup "rollback that stays unhealthy reports 2 with the manual command"
echo fail >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2
assert_contains "$OUT" "SERVICE IS DOWN"
assert_contains "$OUT" "cd $T/compose && docker tag hub-web:pre-$SHORT hub-web:latest"
teardown

setup "compose up failure rolls back"
echo 1 >"$STUB_STATE/up_rc_1"
run_deploy
assert_eq "$RC" 1
assert_contains "$OUT" "docker compose up failed"
teardown

setup "no running container means no rollback point: 2 on failure"
rm "$STUB_STATE/ps/web"
echo fail >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2
assert_contains "$OUT" "web is not running, so it has no rollback point"
assert_contains "$OUT" "nothing can be rolled back"
teardown

setup "first deploy with nothing running still succeeds"
rm "$STUB_STATE/ps/web"
run_deploy
assert_eq "$RC" 0
assert_contains "$OUT" "(no rollback point was recorded)"
teardown

setup "re-running the same commit keeps the earlier rollback point"
echo sha256:EARLIER >"$STUB_STATE/tags/hub-web_pre-$SHORT"
echo sha256:SAMECOMMIT >"$STUB_STATE/inspect/c1"
run_deploy DL_TAG_PUSH=0
assert_eq "$RC" 0
assert_eq "$(cat "$STUB_STATE/tags/hub-web_pre-$SHORT")" "sha256:EARLIER"
assert_contains "$OUT" "keeps hub-web:pre-$SHORT from an earlier run"
teardown

setup "tagged image name keeps its tag on rollback"
echo "registry:5000/team/hub:v2" >"$STUB_STATE/images/web"
echo fail >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_contains "$CALLS" "docker tag sha256:OLDID registry:5000/team/hub:pre-$SHORT"
assert_contains "$CALLS" "docker tag registry:5000/team/hub:pre-$SHORT registry:5000/team/hub:v2"
teardown

setup "registry port without a tag means latest"
echo "registry:5000/hub" >"$STUB_STATE/images/web"
run_deploy DL_TAG_PUSH=0
assert_contains "$OUT" "docker tag registry:5000/hub:pre-$SHORT registry:5000/hub:latest"
teardown

setup "multiple services from compose config"
printf 'web\nworker\n' >"$STUB_STATE/services"
echo hub-worker >"$STUB_STATE/images/worker"
echo c2 >"$STUB_STATE/ps/worker"
echo sha256:WORKERID >"$STUB_STATE/inspect/c2"
echo fail >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_contains "$CALLS" "docker compose up -d --no-build --force-recreate web worker"
assert_eq "$(cat "$STUB_STATE/tags/hub-worker_latest")" "sha256:WORKERID"
teardown

setup "DL_SERVICES limits the deploy"
printf 'web\ndb\n' >"$STUB_STATE/services"
run_deploy DL_SERVICES=web DL_TAG_PUSH=0
assert_contains "$CALLS" "docker compose up -d --build web"
assert_not_contains "$CALLS" "config --images db"
teardown

setup "remote deploy runs docker over ssh with ~ kept expandable"
mkdir -p "$T/home/svc dir"
run_deploy DL_REMOTE=box "HOME='$T/home'" "DL_COMPOSE_DIR='~/svc dir'" DL_TAG_PUSH=0
assert_eq "$RC" 0
assert_contains "$CALLS" 'ssh box cd ~/svc\ dir && docker compose up -d --build'
assert_contains "$OUT" "ssh box 'cd ~/svc dir && docker tag"
teardown

setup "remote health check curls on the box"
run_deploy DL_REMOTE=box DL_HEALTH_FROM=remote DL_TAG_PUSH=0
assert_eq "$RC" 0
assert_contains "$CALLS" "ssh box curl -fsS --max-time 10 http://box/health"
teardown

setup "ssh down before deploy stops with 3 and changes nothing"
touch "$STUB_STATE/ssh_down"
run_deploy DL_REMOTE=box
assert_eq "$RC" 3
assert_not_contains "$CALLS" "up -d"
teardown

setup "empty DL_HEALTH_JQ only needs HTTP success"
echo 'ok plain text OK' >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= DL_TAG_PUSH=0
assert_eq "$RC" 0
teardown

setup "non-JSON body with a predicate is unhealthy"
echo 'ok <html>' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2
teardown

setup "missing settings exit 3"
run_deploy DL_HEALTH_URL=
assert_eq "$RC" 3
assert_contains "$OUT" "missing settings: DL_HEALTH_URL"
teardown

setup "bad DL_HEALTH_FROM exits 3"
run_deploy DL_HEALTH_FROM=box
assert_eq "$RC" 3
teardown

setup "remote health without DL_REMOTE exits 3"
run_deploy DL_HEALTH_FROM=remote
assert_eq "$RC" 3
teardown

setup "dirty tree refused unless allowed"
echo change >>"$T/repo/a.txt"
run_deploy
assert_eq "$RC" 3
assert_contains "$OUT" "uncommitted changes"
assert_not_contains "$CALLS" "docker"
run_deploy DL_ALLOW_DIRTY=1 DL_TAG_PUSH=0
assert_eq "$RC" 0
teardown

setup "unknown DL_SHA exits 3"
run_deploy DL_SHA=deadbeef
assert_eq "$RC" 3
teardown

setup "compose config failure stops before deploying"
touch "$STUB_STATE/fail_config"
run_deploy
assert_eq "$RC" 3
assert_not_contains "$CALLS" "up -d"
teardown

setup "failing to record the pre tag stops before deploying"
touch "$STUB_STATE/fail_tag"
run_deploy
assert_eq "$RC" 3
assert_not_contains "$CALLS" "up -d"
teardown

setup "tag push failure is exit 4 but the deploy stands"
git -C "$T/repo" remote set-url origin "$T/nowhere.git"
run_deploy
assert_eq "$RC" 4
assert_contains "$OUT" "could not push it"
assert_not_contains "$CALLS" "--no-build"
teardown

setup "DL_COMPOSE_ARGS and DL_UP_ARGS pass through"
run_deploy "DL_COMPOSE_ARGS='-f prod.yml -p hub'" "DL_UP_ARGS='up -d --build --force-recreate'" DL_TAG_PUSH=0
assert_contains "$CALLS" "docker compose -f prod.yml -p hub up -d --build --force-recreate"
teardown

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
