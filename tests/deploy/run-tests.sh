#!/usr/bin/env bash
# Tests for deploy/deploy-lib.sh. Plain bash, no bats needed.
# docker, curl, and ssh are stubs (tests/deploy/stubs); git is real, against
# a throwaway repo with a bare "origin", so tag creation and push are real.
# shellcheck disable=SC2034  # DL_* settings are read by the sourced library
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
  export STUB_STATE="$T/state"; mkdir -p "$STUB_STATE"/{images,ps,inspect,tags,deps}
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
  # The version running before the deploy passes the gate (the usual case);
  # tests about a running version that does not, remove or change this.
  echo 'ok {"ok":true}' >"$STUB_STATE/health_pre"
}

# Run dl_run in a subshell with stubs first on PATH. Sets RC and OUT.
run_deploy() {
  OUT=$({
    PATH="$HERE/stubs:$PATH"
    # Only local pushes: a test with a github.com remote must fail its push
    # at once, never reach the network or wait at a credential prompt.
    export GIT_ALLOW_PROTOCOL=file GIT_TERMINAL_PROMPT=0
    # shellcheck source=../../deploy/deploy-lib.sh
    source "$LIB"
    DL_SERVICE=hub DL_COMPOSE_DIR="$T/compose" DL_HEALTH_URL="http://box/health"
    DL_HEALTH_JQ='.ok == true' DL_HEALTH_TIMEOUT=1 DL_HEALTH_INTERVAL=0.05 DL_GIT_DIR="$T/repo"
    DL_STATE_DIR="$T/boxstate" DL_HEALTH_SETTLE=0
    for kv in "$@"; do eval "$kv"; done
    dl_run
    echo "RC=$?"
    echo "TRAPS=$(trap -p EXIT)"
  } 2>&1)
  RC=$(printf '%s\n' "$OUT" | sed -n 's/^RC=//p' | tail -n 1)
  CALLS=$(cat "$STUB_STATE/calls")
  # The pre- tag this run gave the first service's running image.
  PRE=$(printf '%s\n' "$OUT" | sed -n 's/.*rollback point: [^ ]* runs [^ ]*:\(pre-[^ ]*\)$/\1/p' | head -n 1)
}

teardown() { rm -rf "$T"; }

# ---------------------------------------------------------------- tests

setup "healthy deploy records pre tag, deploys, tags git"
run_deploy
assert_eq "$RC" 0
assert_eq "$(cat "$STUB_STATE/tags/hub-web_$PRE")" "sha256:OLDID" "(pre tag points at old image)"
assert_contains "$CALLS" "docker compose up -d --build"
assert_contains "$CALLS" "curl -fsS --max-time 10 http://box/health"
tags=$(git -C "$T/origin.git" tag -l 'deploy/hub/*')
[[ "$tags" =~ ^deploy/hub/[0-9]{8}T[0-9]{6}Z-$SHORT$ ]] && ok || fail "pushed tag looks wrong: '$tags'"
assert_eq "$(git -C "$T/origin.git" rev-parse "$tags^{commit}")" "$SHA" "(tag points at deployed commit)"
assert_contains "$OUT" "docker tag hub-web:$PRE hub-web:latest"
assert_contains "$OUT" "up -d --no-build --force-recreate web"
teardown

setup "unhealthy deploy rolls back and reports 1"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 1
assert_contains "$CALLS" "docker tag hub-web:$PRE hub-web:latest"
assert_contains "$CALLS" "docker compose up -d --no-build --force-recreate web"
assert_eq "$(cat "$STUB_STATE/tags/hub-web_latest")" "sha256:OLDID" "(latest retagged to old image)"
assert_eq "$(git -C "$T/origin.git" tag -l 'deploy/*')" "" "(no deploy tag on failure)"
assert_contains "$OUT" "rolled back, and the old version passes the full health gate (http://box/health)"
teardown

setup "predicate false counts as unhealthy (and a rollback that still answers is degraded, not down)"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 5
assert_contains "$OUT" "DL_HEALTH_JQ predicate is not true"
assert_contains "$OUT" "ROLLED BACK and the old version is UP"
assert_not_contains "$OUT" "SERVICE IS DOWN"
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
assert_contains "$OUT" "cd $T/compose && tar -xzf $T/boxstate/snapshot-"
assert_contains "$OUT" "docker tag hub-web:$PRE hub-web:latest && docker compose up"
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
assert_contains "$OUT" "(no image rollback point was recorded)"
teardown

setup "each run tags what runs now, so redeploying an old commit never reuses a stale tag"
run_deploy DL_TAG_PUSH=0
first=$PRE
echo sha256:NEWER >"$STUB_STATE/inspect/c1"
sleep 1
run_deploy DL_TAG_PUSH=0
[[ "$PRE" != "$first" ]] && ok || fail "pre tag reused: $PRE"
assert_eq "$(cat "$STUB_STATE/tags/hub-web_$PRE")" "sha256:NEWER"
[[ "$PRE" =~ ^pre-[0-9]{8}T[0-9]{6}Z-$SHORT$ ]] && ok || fail "pre tag shape: $PRE"
teardown

setup "tagged image name keeps its tag on rollback"
echo "registry:5000/team/hub:v2" >"$STUB_STATE/images/web"
echo fail >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_contains "$CALLS" "docker tag sha256:OLDID registry:5000/team/hub:$PRE"
assert_contains "$CALLS" "docker tag registry:5000/team/hub:$PRE registry:5000/team/hub:v2"
teardown

setup "registry port without a tag means latest"
echo "registry:5000/hub" >"$STUB_STATE/images/web"
run_deploy DL_TAG_PUSH=0
assert_contains "$OUT" "docker tag registry:5000/hub:$PRE registry:5000/hub:latest"
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
assert_contains "$OUT" "ssh box 'cd ~/svc dir && tar -xzf"
assert_contains "$OUT" "docker tag hub-web:pre-"
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

setup "a shallow health gate is refused before anything happens"
run_deploy DL_HEALTH_JQ=
assert_eq "$RC" 3
assert_contains "$OUT" "health gate is shallow"
assert_not_contains "$CALLS" "docker"
run_deploy DL_HEALTH_JQ=.
assert_eq "$RC" 3
run_deploy DL_HEALTH_JQ= DL_HEALTH_SHALLOW_OK=1 DL_TAG_PUSH=0
assert_eq "$RC" 0
teardown

setup "non-JSON body is unhealthy"
echo 'ok <html>' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 5 "(it answers, so it is not down)"
assert_contains "$OUT" "not JSON"
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

# ---------------------------------------------------------------- real health gate

setup "the vanished-card case: ok:true but a required integration is down rolls back"
printf 'ok {"ok":true,"integrations":{"ha":{"ok":false}}}\nok {"ok":true,"integrations":{"ha":{"ok":true}}}\n' >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.integrations.ha.ok'" DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 1
assert_contains "$OUT" "required .integrations.ha.ok is missing or false"
assert_contains "$CALLS" "--no-build --force-recreate web"
teardown

setup "required fields: all present passes, a missing one fails, empty string fails"
echo 'ok {"a":{"b":1},"c":"x","e":""}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.a.b .c'" DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.a.b .d'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "required .d is missing"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.e'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "required .e is missing or false"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.c.deeper[0]'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "required .c.deeper[0] is missing"
teardown

setup "fresh data: recent Z, offset and epoch ms timestamps pass"
now_z=$(date -u +%Y-%m-%dT%H:%M:%SZ)
now_off=$(date -u -v-7H +%Y-%m-%dT%H:%M:%S.123-07:00 2>/dev/null || date -u -d '-7 hours' +%Y-%m-%dT%H:%M:%S.123-07:00)
now_ms=$(( $(date +%s) * 1000 ))
echo "ok {\"a\":\"$now_z\",\"b\":\"$now_off\",\"c\":$now_ms}" >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_FRESH='.a 300
.b 300
.c 300'" DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
teardown

setup "fresh data: stale, missing and junk timestamps fail with a reason"
echo 'ok {"a":"2026-01-01T00:00:00Z","b":"yesterday"}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_FRESH='.a 300'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "fresh .a is"
assert_contains "$OUT" "min old (limit 300 s)"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_FRESH='.zz 300'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "fresh .zz is missing or not a timestamp"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_FRESH='.b 300'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "fresh .b is missing or not a timestamp"
teardown

setup "a malformed DL_HEALTH_FRESH line is a config error"
run_deploy "DL_HEALTH_FRESH='.a soon'"
assert_eq "$RC" 3
assert_contains "$OUT" "must be '<jq path> <max age seconds>'"
teardown

# ---------------------------------------------------------------- box files

setup "required .env missing: refused before restart, nothing brought up"
run_deploy "DL_REQUIRE_FILES='.env'"
assert_eq "$RC" 3
assert_contains "$OUT" "required on the box: missing or empty: .env"
assert_not_contains "$CALLS" "compose up"
teardown

setup "required env value: empty refused, set passes (plain, quoted, export)"
printf 'HA_TOKEN=\nOTHER=1\n' >"$T/compose/.env"
run_deploy "DL_REQUIRE_ENV='.env:HA_TOKEN'" DL_TAG_PUSH=0
assert_eq "$RC" 3
assert_contains "$OUT" ".env has no value for HA_TOKEN"
for line in 'HA_TOKEN=abc' 'HA_TOKEN="abc"' 'export HA_TOKEN=abc'; do
  printf '%s\n' "$line" >"$T/compose/.env"
  run_deploy "DL_REQUIRE_ENV='.env:HA_TOKEN'" DL_TAG_PUSH=0
  assert_eq "$RC" 0 "($line) $OUT"
done
teardown

setup "a dl_sync that deletes .env is caught, and .env is put back"
printf 'HA_TOKEN=abc\n' >"$T/compose/.env"
run_deploy "dl_sync() { rm -f '$T/compose/.env'; }" "DL_REQUIRE_ENV='.env:HA_TOKEN'"
assert_eq "$RC" 3
assert_contains "$OUT" "putting the files back"
assert_eq "$(cat "$T/compose/.env" 2>/dev/null)" "HA_TOKEN=abc"
assert_not_contains "$CALLS" "compose up"
teardown

setup "dl_sync failure restores files and restarts nothing"
echo old >"$T/compose/config.json"
run_deploy "dl_sync() { echo new >'$T/compose/config.json'; return 1; }"
assert_eq "$RC" 1
assert_eq "$(cat "$T/compose/config.json")" "old"
assert_not_contains "$CALLS" "compose up"
teardown

setup "unhealthy deploy puts synced files back as well as images"
echo old >"$T/compose/config.json"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy "dl_sync() { echo new >'$T/compose/config.json'; }" DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 1
assert_eq "$(cat "$T/compose/config.json")" "old"
teardown

setup "snapshot excludes and keep count"
mkdir -p "$T/compose/data"; echo big >"$T/compose/data/blob"; echo c >"$T/compose/c.yml"
for _ in 1 2 3; do
  run_deploy "DL_SNAPSHOT_EXCLUDE='./data'" DL_SNAPSHOT_KEEP=2 DL_TAG_PUSH=0
  sleep 1
done
n=$(ls "$T/boxstate"/snapshot-*.tgz | wc -l | tr -d ' ')
assert_eq "$n" 2 "(kept snapshots)"
listing=$(tar -tzf "$(ls -t "$T/boxstate"/snapshot-*.tgz | head -n 1)")
assert_contains "$listing" "c.yml"
assert_not_contains "$listing" "blob"
teardown

setup "DL_SNAPSHOT=0 skips the snapshot"
run_deploy DL_SNAPSHOT=0 DL_TAG_PUSH=0
assert_eq "$RC" 0
[[ ! -e "$T/boxstate" ]] || [[ -z "$(ls "$T/boxstate"/snapshot-* 2>/dev/null)" ]] && ok || fail "snapshot written"
teardown

# ---------------------------------------------------------------- git record

setup "success records the deployed commit on the box; the next deploy tags it as the rollback point"
run_deploy
assert_eq "$RC" 0 "$OUT"
assert_eq "$(cat "$T/boxstate/deployed-sha")" "$SHA"
assert_contains "$OUT" "does not record which commit it runs yet"
echo two >>"$T/repo/a.txt"; git -C "$T/repo" commit -qam two
NEW=$(git -C "$T/repo" rev-parse HEAD)
run_deploy
assert_eq "$RC" 0 "$OUT"
rp=$(git -C "$T/origin.git" tag -l 'rollback-point/hub/*')
[[ -n "$rp" ]] && ok || fail "no rollback-point tag pushed"
assert_eq "$(git -C "$T/origin.git" rev-parse "$rp^{commit}")" "$SHA" "(rollback point is the old commit)"
assert_eq "$(cat "$T/boxstate/deployed-sha")" "$NEW"
teardown

setup "a failed deploy is tagged failed-deploy and the box state keeps the old commit"
run_deploy
echo two >>"$T/repo/a.txt"; git -C "$T/repo" commit -qam two
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"; rm -f "$STUB_STATE/curl_count"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 1 "$OUT"
[[ -n "$(git -C "$T/origin.git" tag -l 'failed-deploy/hub/*')" ]] && ok || fail "no failed-deploy tag"
assert_eq "$(cat "$T/boxstate/deployed-sha")" "$SHA"
teardown

setup "a box that names a commit this clone lacks is a warning, not a stop"
mkdir -p "$T/boxstate"; echo "1234567890123456789012345678901234567890" >"$T/boxstate/deployed-sha"
run_deploy
assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "which this clone does not have"
teardown

setup "remote mode keeps state under the box's HOME by default"
mkdir -p "$T/home/svc"
run_deploy DL_REMOTE=box "HOME='$T/home'" "DL_COMPOSE_DIR='~/svc'" DL_STATE_DIR= DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
[[ -n "$(ls "$T/home/.local/state/homelab-deploy/hub/"snapshot-*.tgz 2>/dev/null)" ]] && ok || fail "no remote snapshot"
teardown

# ---------------------------------------------------------------- CLI

setup "homelab-deploy runs a config file and --health checks the gate"
cat >"$T/repo/deploy.conf" <<EOF
DL_SERVICE=hub
DL_COMPOSE_DIR='$T/compose'
DL_HEALTH_URL=http://box/health
DL_HEALTH_REQUIRE=.ok
DL_HEALTH_TIMEOUT=1
DL_HEALTH_INTERVAL=0.05
DL_STATE_DIR='$T/boxstate'
DL_TAG_PUSH=0
EOF
git -C "$T/repo" add deploy.conf; git -C "$T/repo" commit -qm conf
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/deploy.conf" 2>&1); RC=$?
assert_eq "$RC" 0 "$OUT"
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/deploy.conf" --health 2>&1); RC=$?
assert_eq "$RC" 0 "$OUT"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/deploy.conf" --health 2>&1); RC=$?
assert_eq "$RC" 1
assert_contains "$OUT" "required .ok is missing or false"
OUT=$(bash "$HERE/../../deploy/homelab-deploy" "$T/nope.conf" 2>&1); assert_eq "$?" 3
teardown

# ---------------------------------------------------------------- review follow-ups

setup "DL_SHA other than HEAD is refused (the checkout is what ships)"
echo two >>"$T/repo/a.txt"; git -C "$T/repo" commit -qam two
run_deploy "DL_SHA=$SHA"
assert_eq "$RC" 3
assert_contains "$OUT" "is not HEAD"
run_deploy DL_SHA=HEAD DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
teardown

setup "bad settings are config errors (3) before anything happens"
for kv in DL_SNAPSHOT_KEEP=0 DL_SNAPSHOT_KEEP=x DL_HEALTH_TIMEOUT=abc DL_HEALTH_SETTLE=soon \
          DL_HEALTH_INTERVAL=x DL_SERVICE=a/b DL_SERVICE=../x "DL_HEALTH_REQUIRE='.a['" \
          "DL_HEALTH_JQ='.ok =='" DL_HEALTH_JQ=true "DL_HEALTH_REQUIRE='   '"; do
  : >"$STUB_STATE/calls"
  run_deploy "$kv" "$( [[ $kv == DL_HEALTH_REQUIRE=* ]] && echo DL_HEALTH_JQ= )"
  assert_eq "$RC" 3 "($kv) ${OUT:0:300}"
  assert_not_contains "$(cat "$STUB_STATE/calls")" "docker" "($kv)"
done
teardown

setup "a jq path with quoted keys works and is reported readably"
echo 'ok {"home-assistant":{"ok":false}}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.\"home-assistant\".ok'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" 'required ."home-assistant".ok is missing or false'
teardown

setup "paths that yield nothing, or several values, are judged on every value"
echo 'ok {"checks":[],"many":[{"ok":true},{"ok":false}],"all":[{"ok":true},{"ok":true}]}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.checks[].ok'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "required .checks[].ok is missing"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.many[].ok'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "required .many[].ok is missing or false"
rm -f "$STUB_STATE/curl_count"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.all[].ok'" DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
teardown

setup "a DL_HEALTH_JQ written with select fails when select drops everything"
echo 'ok {"status":"down"}' >"$STUB_STATE/health"
run_deploy "DL_HEALTH_JQ='.status | select(. == \"ok\")'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "DL_HEALTH_JQ predicate is not true"
teardown

setup "DL_HEALTH_COMMIT proves the new version answered"
echo "ok {\"ok\":true,\"commit\":\"$SHORT\"}" >"$STUB_STATE/health"
run_deploy "DL_HEALTH_COMMIT=.commit" DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
echo 'ok {"ok":true,"commit":"0123456789ab"}' >"$STUB_STATE/health"; rm -f "$STUB_STATE/curl_count"
run_deploy "DL_HEALTH_COMMIT=.commit" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "not the deployed $SHORT"
echo 'ok {"ok":true,"commit":"ab"}' >"$STUB_STATE/health"; rm -f "$STUB_STATE/curl_count"
run_deploy "DL_HEALTH_COMMIT=.commit" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "not the deployed" "(too-short commit never matches)"
teardown

setup "settle: healthy once, then failing, is unhealthy"
printf 'ok {"ok":true}\nfail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy DL_HEALTH_SETTLE=0.1 DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "then not healthy"
teardown

setup "the version running now is probed first and a failure is only a warning"
rm "$STUB_STATE/health_pre"
run_deploy DL_TAG_PUSH=0
assert_eq "$RC" 0
assert_contains "$OUT" "the version running now does not pass the health gate"
echo 'ok {"ok":true}' >"$STUB_STATE/health_pre"
run_deploy DL_TAG_PUSH=0
assert_not_contains "$OUT" "does not pass the health gate"
teardown

setup "fresh data: epoch seconds, +05:30 offsets and fractional Z all work"
now_s=$(date +%s)
plus=$(date -u -v+5H -v+30M +%Y-%m-%dT%H:%M:%S+05:30 2>/dev/null || date -u -d '+5 hours 30 minutes' +%Y-%m-%dT%H:%M:%S+05:30)
fracz=$(date -u +%Y-%m-%dT%H:%M:%S.987654Z)
echo "ok {\"a\":$now_s,\"b\":\"$plus\",\"c\":\"$fracz\"}" >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_FRESH='.a 120
.b 120
.c 120'" DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
teardown

setup "fresh data: a stale value fails the deploy; a far-future value fails too"
echo 'ok {"a":"2026-01-01T00:00:00Z","f":"2099-01-01T00:00:00Z"}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_FRESH='.a 300'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 1 "(the running version was stale before the deploy too, so its rollback is judged by liveness)"
assert_contains "$OUT" "judging the rollback by liveness (http://box/health)"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_FRESH='.f 300'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "fresh .f is in the future"
teardown

setup "required env edge cases"
for body in 'HA_TOKEN=""' $'HA_TOKEN=abc\nHA_TOKEN=' 'HA_TOKEN= # fill me in'; do
  printf '%s\n' "$body" >"$T/compose/.env"
  run_deploy "DL_REQUIRE_ENV='.env:HA_TOKEN'" DL_TAG_PUSH=0
  assert_eq "$RC" 3 "($body)"
done
rm "$T/compose/.env"
run_deploy "DL_REQUIRE_ENV='.env:HA_TOKEN'" DL_TAG_PUSH=0
assert_contains "$OUT" "missing or empty: .env (needed for HA_TOKEN)"
: >"$T/compose/config.json"
run_deploy "DL_REQUIRE_FILES='config.json'" DL_TAG_PUSH=0
assert_eq "$RC" 3 "(zero-byte required file)"
teardown

setup "remote mode: a sync that deletes .env in a dir with a space is caught and undone"
mkdir -p "$T/home/svc dir"; printf 'HA_TOKEN=abc\n' >"$T/home/svc dir/.env"
run_deploy DL_REMOTE=box "HOME='$T/home'" "DL_COMPOSE_DIR='~/svc dir'" DL_STATE_DIR= \
  "DL_REQUIRE_ENV='.env:HA_TOKEN'" "dl_sync() { rm -f '$T/home/svc dir/.env'; }"
assert_eq "$RC" 3 "$OUT"
assert_eq "$(cat "$T/home/svc dir/.env" 2>/dev/null)" "HA_TOKEN=abc"
run_deploy DL_REMOTE=box "HOME='$T/home'" "DL_COMPOSE_DIR='~/svc dir'" DL_STATE_DIR= \
  "DL_REQUIRE_ENV='.env:HA_TOKEN'"
assert_eq "$RC" 0 "$OUT"
assert_eq "$(cat "$T/home/.local/state/homelab-deploy/hub/deployed-sha")" "$SHA"
teardown

setup "no rollback point still puts the synced files back"
rm "$STUB_STATE/ps/web"; echo old >"$T/compose/config.json"
echo fail >"$STUB_STATE/health"
run_deploy "dl_sync() { echo new >'$T/compose/config.json'; }" DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2
assert_eq "$(cat "$T/compose/config.json")" "old"
teardown

setup "a snapshot that cannot be written stops before anything restarts"
touch "$T/notadir"
run_deploy "DL_STATE_DIR='$T/notadir'"
assert_eq "$RC" 3
assert_not_contains "$CALLS" "compose up"
teardown

setup "if the files cannot be restored the images are still rolled back, and it says so (5)"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy "dl_sync() { rm -f '$T'/boxstate/snapshot-*.tgz; }" DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 5 "$OUT"
assert_contains "$OUT" "compose directory was not restored"
assert_contains "$CALLS" "--no-build --force-recreate web"
teardown

setup "a service with no rollback point that stays on the new build makes it exit 5"
printf 'web\nworker\n' >"$STUB_STATE/services"; echo hub-worker >"$STUB_STATE/images/worker"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 5 "$OUT"
assert_contains "$OUT" "still run the new build: worker"
teardown

setup "a failed deploy tags the failed commit and leaves the box record alone"
run_deploy
echo two >>"$T/repo/a.txt"; git -C "$T/repo" commit -qam two
NEW=$(git -C "$T/repo" rev-parse HEAD)
echo fail >"$STUB_STATE/health"; rm -f "$STUB_STATE/curl_count"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2
ft=$(git -C "$T/origin.git" tag -l 'failed-deploy/hub/*')
assert_eq "$(git -C "$T/origin.git" rev-parse "$ft^{commit}")" "$NEW"
assert_eq "$(cat "$T/boxstate/deployed-sha")" "$SHA"
teardown

setup "a deployed-commit record that cannot be written is exit 4"
run_deploy "dl_write_deployed_sha() { return 1; }" DL_TAG_PUSH=0
assert_eq "$RC" 4 "$OUT"
teardown

setup "dying mid-deploy prints what state it was in and the rollback command"
# Through homelab-deploy, as a real deploy runs (its own process, not a
# command substitution: bash 3.2 skips EXIT traps inside those).
cat >"$T/repo/die.conf" <<EOF
DL_SERVICE=hub
DL_COMPOSE_DIR='$T/compose'
DL_HEALTH_URL=http://box/health
DL_HEALTH_REQUIRE=.ok
DL_STATE_DIR='$T/boxstate'
DL_TAG_PUSH=0
dl_wait_healthy() { exit 7; }
EOF
git -C "$T/repo" add die.conf; git -C "$T/repo" commit -qm die
PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/die.conf" >"$T/die.out" 2>&1
RC=$?; OUT=$(cat "$T/die.out")
assert_eq "$RC" 7
assert_contains "$OUT" "deploy ABORTED during 'checking'"
assert_contains "$OUT" "docker tag hub-web:pre-"
teardown

setup "homelab-deploy refuses a config that does not load, a shallow --health, and unknown modes"
printf 'DL_SERVICE=hub\nif then\n' >"$T/repo/broken.conf"
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/broken.conf" 2>&1); RC=$?
assert_eq "$RC" 3 "$OUT"
assert_contains "$OUT" "failed to load"
printf "DL_SERVICE=hub\nDL_COMPOSE_DIR='%s'\nDL_HEALTH_URL=http://box/health\n" "$T/compose" >"$T/repo/shallow.conf"
git -C "$T/repo" add -A; git -C "$T/repo" commit -qm confs
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/shallow.conf" --health 2>&1); RC=$?
assert_eq "$RC" 3 "$OUT"
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/shallow.conf" --bogus 2>&1); RC=$?
assert_eq "$RC" 3 "$OUT"
teardown

setup "with DL_HEALTH_COMMIT a rollback is judged against the old commit"
echo "ok {\"ok\":true,\"commit\":\"$SHORT\"}" >"$STUB_STATE/health"
echo "ok {\"ok\":true,\"commit\":\"$SHORT\"}" >"$STUB_STATE/health_pre"
run_deploy DL_HEALTH_COMMIT=.commit
assert_eq "$RC" 0 "$OUT"
echo two >>"$T/repo/a.txt"; git -C "$T/repo" commit -qam two
rm -f "$STUB_STATE/curl_count"
# The new build never comes up: the old container keeps answering with the old commit.
run_deploy DL_HEALTH_COMMIT=.commit DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "rolled back, and the old version passes the full health gate"
assert_not_contains "$OUT" "does not pass the health gate"
teardown

setup "pruning keeps hand-made tags and the tag this run made"
for t in pre-port-abc pre-radar-v4 pre-20260101T000000Z-aaaaaaaaaaaa pre-20260102T000000Z-bbbbbbbbbbbb pre-20260103T000000Z-cccccccccccc; do
  echo sha256:X >"$STUB_STATE/tags/hub-web_$t"
done
run_deploy DL_SNAPSHOT_KEEP=2 DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
left=$(for f in "$STUB_STATE/tags"/hub-web_pre-*; do printf '%s ' "${f##*/}"; done)
assert_contains "$left" "hub-web_pre-port-abc"
assert_contains "$left" "hub-web_pre-radar-v4"
assert_contains "$left" "hub-web_$PRE"
assert_contains "$left" "hub-web_pre-20260103T000000Z-cccccccccccc"
assert_not_contains "$left" "pre-20260101T000000Z"
assert_not_contains "$left" "pre-20260102T000000Z"
teardown

# --- the image a service runs (2.2.1) -----------------------------------------
# `compose config --images web` also lists the images of what web depends_on,
# in no set order. On 2026-09-23 house-climate's web came back with the db
# image first, so the running WEB image was tagged timescale/timescaledb:pre-...

setup "a depends_on image listed first is never taken for the service's image (house-climate shape)"
printf 'db\npoller\nweb\n' >"$STUB_STATE/services"
echo "house-climate" >"$STUB_STATE/project"
echo "timescale/timescaledb:2.29.1-pg16" >"$STUB_STATE/images/db"
: >"$STUB_STATE/images/web"; : >"$STUB_STATE/images/poller"   # build-only, like the real ones
echo db >"$STUB_STATE/deps/web"; echo db >"$STUB_STATE/deps/poller"
echo c-poller >"$STUB_STATE/ps/poller"; echo sha256:OLDPOLLER >"$STUB_STATE/inspect/c-poller"
echo sha256:DBID >"$STUB_STATE/tags/timescale_timescaledb_2.29.1-pg16"
# The trap is real: the old way reads the db image first.
first=$(PATH="$HERE/stubs:$PATH" docker compose config --images web | head -n 1)
assert_eq "$first" "timescale/timescaledb:2.29.1-pg16" "(stub lists the dependency first)"
: >"$STUB_STATE/calls"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy "DL_SERVICES='poller web'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "rollback point: web runs house-climate-web:$PRE"
assert_contains "$OUT" "rollback point: poller runs house-climate-poller:$PRE"
assert_eq "$(cat "$STUB_STATE/tags/house-climate-web_$PRE" 2>/dev/null)" "sha256:OLDID" "(web's own image is tagged)"
assert_not_contains "$(ls "$STUB_STATE/tags")" "timescale_timescaledb_pre-"
assert_contains "$CALLS" "docker tag house-climate-web:$PRE house-climate-web:latest"
assert_eq "$(cat "$STUB_STATE/tags/timescale_timescaledb_2.29.1-pg16")" "sha256:DBID" "(the database image name is left alone)"
assert_eq "$(cat "$STUB_STATE/tags/house-climate-web_latest")" "sha256:OLDID" "(web rolled back onto its own old image)"
assert_contains "$CALLS" "docker compose config --no-env-resolution --format json web"
assert_not_contains "$CALLS" "config --images"
teardown

setup "a service with its own image: and a dependency keeps its own image (fleet-dashboard and family-hub shape)"
printf 'victoriametrics\nweb\n' >"$STUB_STATE/services"
echo "victoriametrics/victoria-metrics:v1.102.1" >"$STUB_STATE/images/victoriametrics"
echo "family-hub:1.0" >"$STUB_STATE/images/web"
echo victoriametrics >"$STUB_STATE/deps/web"
echo fail >"$STUB_STATE/health"
run_deploy DL_SERVICES=web DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "rollback point: web runs family-hub:$PRE"
assert_contains "$CALLS" "docker tag family-hub:$PRE family-hub:1.0"
assert_not_contains "$(ls "$STUB_STATE/tags")" "victoria-metrics_pre-"
assert_contains "$OUT" "docker tag family-hub:$PRE family-hub:1.0 && docker compose up -d --no-build --force-recreate web"
teardown

setup "a build-only service is <project>-<service>, as compose names what it builds"
: >"$STUB_STATE/images/web"
echo "fleet-dashboard" >"$STUB_STATE/project"
run_deploy DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "rollback point: web runs fleet-dashboard-web:$PRE"
assert_eq "$(cat "$STUB_STATE/tags/fleet-dashboard-web_$PRE")" "sha256:OLDID"
teardown

setup "no single image name for a service stops before anything restarts"
# Each case: the config names no such service; a service with neither image
# nor build; not JSON; the command fails; an image with a space in it.
for cfg in '{"name":"hub","services":{"other":{"image":"x"}}}' \
           '{"name":"hub","services":{"web":{"command":"x"}}}' \
           'not json' \
           '{"name":"hub","services":{"web":{"image":"a b"}}}' \
           '{"services":{"web":{"build":{"context":"."}}}}'; do
  printf '%s\n' "$cfg" >"$STUB_STATE/config_json"; : >"$STUB_STATE/calls"
  run_deploy DL_TAG_PUSH=0
  assert_eq "$RC" 3 "(config $cfg) $OUT"
  assert_contains "$OUT" "could not read the image name for web"
  assert_not_contains "$CALLS" "up -d"
  assert_not_contains "$CALLS" "docker tag"
done
assert_contains "$OUT" "no project name"
rm -f "$STUB_STATE/config_json"; echo 1 >"$STUB_STATE/json_rc"; : >"$STUB_STATE/calls"
run_deploy DL_TAG_PUSH=0
assert_eq "$RC" 3 "$OUT"
assert_contains "$OUT" "docker compose config --format json web failed"
assert_not_contains "$CALLS" "up -d"
teardown

setup "the image name comes over ssh for a remote box too"
echo "house-climate" >"$STUB_STATE/project"; : >"$STUB_STATE/images/web"
printf 'db\nweb\n' >"$STUB_STATE/services"; echo "timescale/timescaledb:2.29.1-pg16" >"$STUB_STATE/images/db"
echo db >"$STUB_STATE/deps/web"
mkdir -p "$T/home/box"
run_deploy DL_REMOTE=box "HOME='$T/home'" "DL_COMPOSE_DIR='~/box'" DL_SERVICES=web DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
assert_contains "$CALLS" "ssh box cd ~/box && docker compose config --no-env-resolution --format json web"
assert_contains "$OUT" "rollback point: web runs house-climate-web:$PRE"
teardown

# --- pruning old pre- tags ------------------------------------------------------

setup "pruning keeps the newest DL_SNAPSHOT_KEEP per repo, this run's included, and only exact library tags"
for t in pre-20260101T000000Z-aaaaaaaaaaaa pre-20260102T000000Z-bbbbbbbbbbbb pre-20260103T000000Z-cccccccccccc \
         pre-20260104T000000Z-dddddddddddd pre-20260105T000000Z-eeeeeeeeeeee \
         pre-20260101T000000Z-abc pre-20260101T000000Z-aaaaaaaaaaaaff pre-20260101T000000Z-AAAAAAAAAAAA \
         pre-20260101T0000Z-aaaaaaaaaaaa xpre-20260101T000000Z-aaaaaaaaaaaa pre-port-edd52cd 1.0; do
  echo sha256:X >"$STUB_STATE/tags/hub-web_$t"
done
run_deploy DL_SNAPSHOT_KEEP=3 DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
left=$(ls "$STUB_STATE/tags" | tr '\n' ' ')
assert_contains "$left" "hub-web_$PRE "
assert_contains "$left" "hub-web_pre-20260105T000000Z-eeeeeeeeeeee "
assert_contains "$left" "hub-web_pre-20260104T000000Z-dddddddddddd "
for gone in 20260101T000000Z-aaaaaaaaaaaa 20260102T000000Z-bbbbbbbbbbbb 20260103T000000Z-cccccccccccc; do
  assert_not_contains "$left" "hub-web_pre-$gone "
  assert_contains "$OUT" "pruned old rollback tag hub-web:pre-$gone"
done
# Not this library's exact shape: never touched.
for kept in pre-20260101T000000Z-abc pre-20260101T000000Z-aaaaaaaaaaaaff pre-20260101T000000Z-AAAAAAAAAAAA \
            pre-20260101T0000Z-aaaaaaaaaaaa xpre-20260101T000000Z-aaaaaaaaaaaa pre-port-edd52cd 1.0; do
  assert_contains "$left" "hub-web_$kept "
done
teardown

setup "pruning never touches another repo's tags, and prunes a shared repo once"
printf 'web\nworker\n' >"$STUB_STATE/services"
echo hub-web >"$STUB_STATE/images/worker"   # two services, one image repo
echo c2 >"$STUB_STATE/ps/worker"; echo sha256:WORKERID >"$STUB_STATE/inspect/c2"
for t in pre-20260101T000000Z-aaaaaaaaaaaa pre-20260102T000000Z-bbbbbbbbbbbb; do
  echo sha256:X >"$STUB_STATE/tags/hub-web_$t"
  echo sha256:X >"$STUB_STATE/tags/timescale_timescaledb_$t"
done
run_deploy DL_SNAPSHOT_KEEP=1 DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
left=$(ls "$STUB_STATE/tags" | tr '\n' ' ')
assert_contains "$left" "timescale_timescaledb_pre-20260101T000000Z-aaaaaaaaaaaa "
assert_contains "$left" "timescale_timescaledb_pre-20260102T000000Z-bbbbbbbbbbbb "
assert_not_contains "$left" "hub-web_pre-2026010"
assert_contains "$left" "hub-web_$PRE "
assert_eq "$(grep -c 'image ls' "$STUB_STATE/calls")" 1 "(one listing for the shared repo)"
teardown

setup "a failed deploy prunes nothing"
for t in pre-20260101T000000Z-aaaaaaaaaaaa pre-20260102T000000Z-bbbbbbbbbbbb; do
  echo sha256:X >"$STUB_STATE/tags/hub-web_$t"
done
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy DL_SNAPSHOT_KEEP=1 DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 1 "$OUT"
assert_not_contains "$CALLS" "docker rmi"
assert_not_contains "$CALLS" "image ls"
teardown

setup "pruning that cannot list or remove tags warns, and the deploy still succeeds"
echo sha256:X >"$STUB_STATE/tags/hub-web_pre-20260101T000000Z-aaaaaaaaaaaa"
touch "$STUB_STATE/fail_image_ls"
run_deploy DL_SNAPSHOT_KEEP=1 DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "could not list the tags of hub-web, so its old pre- tags were not pruned"
assert_contains "$OUT" "Cannot connect to the Docker daemon"
rm -f "$STUB_STATE/fail_image_ls"; touch "$STUB_STATE/fail_rmi"
run_deploy DL_SNAPSHOT_KEEP=1 DL_TAG_PUSH=0
assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "could not remove old tag hub-web:pre-20260101T000000Z-aaaaaaaaaaaa"
assert_contains "$OUT" "unable to remove repository reference"
assert_contains "$OUT" "deploy OK"
teardown

setup "the caller's own EXIT trap survives dl_run"
run_deploy "trap 'echo caller-cleanup' EXIT" DL_TAG_PUSH=0
assert_eq "$RC" 0
assert_contains "$OUT" "TRAPS=trap -- 'echo caller-cleanup' EXIT"
teardown

setup "a config that ends in a false test still loads"
cat >"$T/repo/ok.conf" <<EOF
DL_SERVICE=hub
DL_COMPOSE_DIR='$T/compose'
DL_HEALTH_URL=http://box/health
DL_HEALTH_REQUIRE=.ok
DL_HEALTH_SETTLE=0
DL_STATE_DIR='$T/boxstate'
DL_TAG_PUSH=0
[ -f /nonexistent ] && DL_SERVICES=web
EOF
git -C "$T/repo" add ok.conf; git -C "$T/repo" commit -qm ok
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/ok.conf" 2>&1); RC=$?
assert_eq "$RC" 0 "$OUT"
teardown

setup "a remote rollback puts back EVERY service, not just the first (2.1.1)"
printf 'go2rtc\nweb\nwyze\n' >"$STUB_STATE/services"
for svc in go2rtc wyze; do
  echo "hub-$svc" >"$STUB_STATE/images/$svc"; echo "c-$svc" >"$STUB_STATE/ps/$svc"
  echo "sha256:OLD-$svc" >"$STUB_STATE/inspect/c-$svc"
done
echo fail >"$STUB_STATE/health"
mkdir -p "$T/home/box"
run_deploy DL_REMOTE=box "HOME='$T/home'" "DL_COMPOSE_DIR='~/box'" DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$CALLS" "docker tag hub-go2rtc:$PRE hub-go2rtc:latest"
assert_contains "$CALLS" "docker tag hub-web:$PRE hub-web:latest"
assert_contains "$CALLS" "docker tag hub-wyze:$PRE hub-wyze:latest"
assert_contains "$CALLS" "up -d --no-build --force-recreate go2rtc web wyze"
assert_eq "$(cat "$STUB_STATE/tags/hub-web_latest")" "sha256:OLDID" "(web really back on its old image)"
teardown

setup "a failed check carries the app's own problems"
echo 'ok {"ok":false,"problems":["laundry: needs_auth (HA_TOKEN is empty)","weather: stale"]}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "DL_HEALTH_JQ predicate is not true (the app says: laundry: needs_auth (HA_TOKEN is empty); weather: stale)"
teardown

setup "a body with no problems list adds nothing"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_not_contains "$OUT" "the app says"
teardown

# ------------------------------------------------ dl_health_extra (2.1.0)
# These hooks count their calls after the restart; the probe of the version
# running before the deploy (DL_PROBE=pre) runs them too, so they skip it.

setup "dl_health_extra runs after the JSON passes, with the body and the commit"
run_deploy DL_TAG_PUSH=0 "dl_health_extra() { [ \"\${DL_PROBE:-}\" = pre ] && return 0; printf '%s|%s\n' \"\$1\" \"\$DL_FULL_SHA\" >>'$T/hook'; }"
assert_eq "$RC" 0
assert_eq "$(cat "$T/hook")" "{\"ok\":true}|$SHA" "(one call, body and deployed commit)"
teardown

setup "a failing dl_health_extra rolls back, and its output is the reason"
run_deploy DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0 \
  "dl_health_extra() { [ \"\${DL_PROBE:-}\" = pre ] && return 0; n=\$(( \$(cat '$T/n' 2>/dev/null || echo 0) + 1 )); echo \$n >'$T/n'; [ \$n -gt 1 ] && return 0; echo noise; echo 'web published on 0.0.0.0' >&2; return 5; }"
assert_eq "$RC" 1
assert_contains "$OUT" "dl_health_extra failed (exit 5): noise web published on 0.0.0.0"
assert_contains "$CALLS" "docker compose up -d --no-build --force-recreate web"
assert_contains "$OUT" "rolled back, and the old version passes the full health gate"
teardown

setup "dl_health_extra is not run while the JSON gate fails"
printf 'ok {"ok":false}\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=5 DL_TAG_PUSH=0 "dl_health_extra() { [ \"\${DL_PROBE:-}\" = pre ] && return 0; echo x >>'$T/hook'; }"
assert_eq "$RC" 0
assert_eq "$(wc -l <"$T/hook" | tr -d ' ')" "1" "(only the probe whose JSON passed ran the hook)"
teardown

setup "dl_health_extra cannot change the deploy's variables"
run_deploy DL_TAG_PUSH=0 "dl_health_extra() { DL_PRE=clobbered; DL_SERVICE=other; }"
assert_eq "$RC" 0
assert_contains "$OUT" "docker tag hub-web:"
assert_not_contains "$OUT" "clobbered"
teardown

setup "the settle recheck runs dl_health_extra again"
run_deploy DL_TAG_PUSH=0 DL_HEALTH_SETTLE=0.1 \
  "dl_health_extra() { [ \"\${DL_PROBE:-}\" = pre ] && return 0; n=\$(( \$(cat '$T/n' 2>/dev/null || echo 0) + 1 )); echo \$n >'$T/n'; [ \$n -lt 2 ] || [ \$n -gt 2 ]; }"
assert_eq "$RC" 1 "(healthy once, the hook failed on the settle check, rollback healthy)"
assert_contains "$OUT" "healthy once, then not healthy"
teardown

setup "a hook alone is still a shallow gate"
run_deploy DL_HEALTH_JQ= "dl_health_extra() { return 0; }"
assert_eq "$RC" 3
assert_contains "$OUT" "the health gate is shallow"
teardown

setup "--health runs dl_health_extra and reports why it failed"
cat >"$T/repo/hook.conf" <<EOF
DL_SERVICE=hub
DL_COMPOSE_DIR='$T/compose'
DL_HEALTH_URL=http://box/health
DL_HEALTH_REQUIRE=.ok
DL_STATE_DIR='$T/boxstate'
dl_health_extra() { echo "board did not paint"; return 1; }
EOF
git -C "$T/repo" add hook.conf; git -C "$T/repo" commit -qm hook
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/hook.conf" --health 2>&1); RC=$?
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "not healthy: dl_health_extra failed (exit 1): board did not paint"
teardown

# ------------------------------------------------ judging a rollback (2.2.0)

# The exact 2026-09-23 family-hub sequence: the hub running before the
# deploy has no deploy record, so the new gate's .config.matches_deploy is
# false for it and always will be; the new build fails the gate; the
# rollback puts the old hub back, up and serving. It must be judged by
# liveness, report exit 1 with that gate named, never say DOWN, and not sit
# out the timeout waiting for a gate the old version can never pass.
setup "a version without a deploy record: failed deploy, rollback judged by liveness, exit 1, no DOWN, no long wait"
old_body='ok {"status":"ok","config":{"matches_deploy":false}}'
echo "$old_body" >"$STUB_STATE/health_pre"
echo "$old_body" >"$STUB_STATE/health"   # the new build fails too, and so would the old one, forever
[[ ! -e "$T/boxstate/deployed-sha" ]] && ok || fail "the box must start with no deploy record"
t0=$SECONDS
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.config.matches_deploy'" DL_LIVENESS_URL=http://box/live \
  DL_HEALTH_TIMEOUT=3 DL_HEALTH_INTERVAL=0.2
elapsed=$(( SECONDS - t0 ))
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "the version running now does not pass the health gate (3 tries): required .config.matches_deploy is missing or false"
assert_contains "$OUT" "so if this deploy fails, its rollback is judged by liveness (http://box/live)"
assert_contains "$OUT" "not healthy after 3s"
assert_contains "$OUT" "judging the rollback by liveness (http://box/live)"
assert_contains "$OUT" "healthy after 1 check(s): liveness (http://box/live)"
assert_contains "$OUT" "rolled back, and the old version passes liveness (http://box/live)"
assert_not_contains "$OUT" "SERVICE IS DOWN"
assert_not_contains "$OUT" "ROLLBACK FAILED"
assert_contains "$CALLS" "docker compose up -d --no-build --force-recreate web"
(( elapsed < 6 )) && ok || fail "the whole run took ${elapsed}s: the rollback waited out a gate it could never pass"
log=$(cat "$T/boxstate/deploys.log")
assert_contains "$log" "failed-deploy sha=$SHA prev=none"
assert_contains "$log" "judged-by=live"
assert_contains "$log" "result=rolled-back rc=1"
[[ ! -e "$T/boxstate/deployed-sha" ]] && ok || fail "a failed deploy must not write a deploy record"
teardown

setup "the same sequence where the old version does not come back: liveness fails, DOWN, exit 2"
echo 'ok {"status":"ok","config":{"matches_deploy":false}}' >"$STUB_STATE/health_pre"
echo fail >"$STUB_STATE/health"
echo fail >"$STUB_STATE/live"
run_deploy DL_HEALTH_JQ= "DL_HEALTH_REQUIRE='.config.matches_deploy'" DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2 "$OUT"
assert_contains "$OUT" "ROLLBACK FAILED. SERVICE IS DOWN: liveness http://box/live: request failed"
assert_contains "$OUT" "Run this by hand, then check http://box/live"
assert_contains "$OUT" "docker tag hub-web:$PRE hub-web:latest"
assert_contains "$(cat "$T/boxstate/deploys.log")" "result=down rc=2"
teardown

setup "a running version that passed the full gate is held to it; failing it but answering is 5, not DOWN"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 5 "$OUT"
assert_contains "$OUT" "passes the full health gate, so a rollback must pass it again"
assert_contains "$OUT" "judging the rollback by the full health gate (http://box/health)"
assert_contains "$OUT" "ROLLED BACK and the old version is UP (liveness http://box/live answers), but it does not pass the full health gate"
assert_not_contains "$OUT" "SERVICE IS DOWN"
assert_contains "$(cat "$T/boxstate/deploys.log")" "judged-by=full"
teardown

setup "full gate, old version fails it and liveness too: DOWN, exit 2"
echo fail >"$STUB_STATE/health"; echo fail >"$STUB_STATE/live"
run_deploy DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 2 "$OUT"
assert_contains "$OUT" "SERVICE IS DOWN"
assert_not_contains "$OUT" "it did not answer liveness before this deploy either"
teardown

setup "without DL_LIVENESS_URL, liveness is the health URL answering at all"
echo 'ok {"ok":false}' >"$STUB_STATE/health_pre"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "passes liveness (http://box/health)"
teardown

setup "a running version that also failed liveness: the rollback is still judged by liveness, and says so if it stays down"
echo fail >"$STUB_STATE/live_pre"; echo fail >"$STUB_STATE/live"
rm "$STUB_STATE/health_pre"; echo fail >"$STUB_STATE/health"
run_deploy DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 2 "$OUT"
assert_contains "$OUT" "it does not answer liveness either"
assert_contains "$OUT" "(it did not answer liveness before this deploy either)"
teardown

setup "the old version passed the JSON gate but not dl_health_extra: the rollback is judged by the JSON gate alone"
run_deploy DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0 \
  "dl_health_extra() { echo \"\${DL_PROBE:-post}\" >>'$T/hook'; echo 'wyze-bridge exited'; return 1; }"
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "the version running now does not pass the health gate (3 tries): dl_health_extra failed (exit 1): wyze-bridge exited"
assert_contains "$OUT" "judging the rollback by the health gate without dl_health_extra (http://box/health)"
assert_eq "$(tr '\n' ' ' <"$T/hook")" "pre pre pre post " "(the hook ran for the three pre tries and the new build, never for the rollback)"
teardown

setup "the old version failed the JSON gate but passed dl_health_extra: the rollback must pass liveness AND the hook"
echo 'ok {"ok":false}' >"$STUB_STATE/health_pre"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0 \
  "dl_health_extra() { [ \"\${DL_PROBE:-}\" = pre ] && return 0; [ \"\$1\" = '' ] || { echo \"body passed: \$1\"; return 1; }; echo 'go2rtc: is exited'; return 1; }"
assert_eq "$RC" 5 "$OUT"
assert_contains "$OUT" "its rollback is judged by liveness (http://box/live) plus dl_health_extra"
assert_contains "$OUT" "go2rtc: is exited"
assert_contains "$OUT" "ROLLED BACK and the old version is UP"
assert_not_contains "$OUT" "body passed"
teardown

setup "no rollback point and the new build still answers: 5, not DOWN"
rm "$STUB_STATE/ps/web"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 5 "$OUT"
assert_contains "$OUT" "the NEW build still runs and failed the gate"
assert_not_contains "$OUT" "SERVICE IS DOWN"
teardown

setup "a rollback whose docker compose fails: 5 when something answers, 2 when nothing does"
echo 1 >"$STUB_STATE/up_rc_2"
echo fail >"$STUB_STATE/health"
run_deploy DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 5 "$OUT"
assert_contains "$OUT" "the rollback did not complete (above), but something answers liveness"
rm -f "$STUB_STATE/up_count"; echo fail >"$STUB_STATE/live"
run_deploy DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_eq "$RC" 2 "$OUT"
assert_contains "$OUT" "ROLLBACK FAILED and liveness does not answer"
teardown

setup "a dl_sync failure with no snapshot to put back is 6: nothing restarted, files half-synced"
run_deploy DL_SNAPSHOT=0 "dl_sync() { return 1; }"
assert_eq "$RC" 6 "$OUT"
assert_contains "$OUT" "dl_sync's changes are still in"
teardown

# ------------------------------------------------ deploys.log and tag privacy (2.2.0)

setup "a healthy deploy is written to deploys.log on the box"
run_deploy
assert_eq "$RC" 0 "$OUT"
assert_contains "$(cat "$T/boxstate/deploys.log")" "hub deployed sha=$SHA prev=none images=pre-"
teardown

setup "DL_TAG_PUSH=local records private refs, not tags, and no release push publishes them"
run_deploy DL_TAG_PUSH=local
assert_eq "$RC" 0 "$OUT"
hist() { git -C "$T/repo" for-each-ref --format='%(refname)' refs/deploy-history; }
assert_contains "$(hist)" "refs/deploy-history/deploy/hub/"
assert_eq "$(git -C "$T/repo" tag -l)" "" "(no tags at all in the clone)"
assert_eq "$(git -C "$T/origin.git" for-each-ref)" "" "(nothing pushed)"
assert_contains "$OUT" "DL_TAG_PUSH=local: a private ref, never pushed"
ref=$(hist | head -n 1)
assert_eq "$(git -C "$T/repo" rev-parse "$ref^{commit}")" "$SHA" "(the record points at the deployed commit)"
assert_contains "$(git -C "$T/repo" cat-file -p "$ref")" "Deployed hub at $SHORT"
echo fail >"$STUB_STATE/health"
echo two >>"$T/repo/a.txt"; git -C "$T/repo" commit -qam two
run_deploy DL_TAG_PUSH=local DL_HEALTH_TIMEOUT=0
assert_contains "$(hist)" "refs/deploy-history/failed-deploy/hub/"
assert_contains "$(hist)" "refs/deploy-history/rollback-point/hub/"
# How the public engines release: a push of main with --follow-tags, and
# sometimes --tags. Neither may carry the deploy history along.
git -C "$T/repo" push -q --follow-tags origin HEAD:refs/heads/main
git -C "$T/repo" push -q --tags origin
assert_eq "$(git -C "$T/origin.git" for-each-ref --format='%(refname)' | grep -v '^refs/heads/')" "" "(a release push published deploy history)"
teardown

setup "a PUBLIC GitHub remote refuses to push deploy tags, before anything changes"
git -C "$T/repo" remote set-url origin https://github.com/someone/public-app.git
echo false >"$STUB_STATE/gh_private"
run_deploy
assert_eq "$RC" 3 "$OUT"
assert_contains "$OUT" "someone/public-app is PUBLIC"
assert_contains "$CALLS" "gh api repos/someone/public-app --jq .private"
assert_not_contains "$CALLS" "docker"
run_deploy DL_TAG_PUSH=local
assert_eq "$RC" 0 "(local tags are fine) $OUT"
run_deploy DL_TAG_PUSH_PUBLIC=1
assert_eq "$RC" 4 "(allowed on purpose; the push itself fails, there is no GitHub here)"
teardown

setup "a private GitHub remote is allowed; an unknown answer or a gh failure is refused"
git -C "$T/repo" remote set-url origin git@github.com:someone/private-app.git
echo true >"$STUB_STATE/gh_private"
run_deploy
assert_eq "$RC" 4 "(the check lets it through; the push fails with no GitHub here) $OUT"
assert_contains "$CALLS" "gh api repos/someone/private-app --jq .private"
touch "$STUB_STATE/gh_fail"; : >"$STUB_STATE/calls"
run_deploy
assert_eq "$RC" 3 "$OUT"
assert_contains "$OUT" "cannot tell whether someone/private-app is private"
assert_not_contains "$(cat "$STUB_STATE/calls")" "docker"
rm "$STUB_STATE/gh_fail"; echo null >"$STUB_STATE/gh_private"
run_deploy
assert_eq "$RC" 3 "$OUT"
teardown

setup "the PUSH URL is what is checked: a private fetch URL with a public push URL is refused"
git -C "$T/repo" config remote.origin.pushurl https://github.com/someone/public-app.git
echo false >"$STUB_STATE/gh_private"
run_deploy
assert_eq "$RC" 3 "$OUT"
assert_contains "$OUT" "someone/public-app is PUBLIC"
git -C "$T/repo" config --unset remote.origin.pushurl
git -C "$T/repo" config --add remote.origin.pushurl "$T/origin.git"
git -C "$T/repo" config --add remote.origin.pushurl https://github.com/someone/public-app.git
run_deploy
assert_eq "$RC" 3 "(every push URL counts, not just the first) $OUT"
teardown

setup "--health makes no tags, so it never asks gh"
git -C "$T/repo" remote set-url origin https://github.com/someone/public-app.git
echo false >"$STUB_STATE/gh_private"
cat >"$T/repo/h.conf" <<EOF
DL_SERVICE=hub
DL_COMPOSE_DIR='$T/compose'
DL_HEALTH_URL=http://box/health
DL_HEALTH_REQUIRE=.ok
DL_STATE_DIR='$T/boxstate'
EOF
git -C "$T/repo" add h.conf; git -C "$T/repo" commit -qm h
: >"$STUB_STATE/calls"
OUT=$(PATH="$HERE/stubs:$PATH" bash "$HERE/../../deploy/homelab-deploy" "$T/repo/h.conf" --health 2>&1); RC=$?
assert_eq "$RC" 0 "$OUT"
assert_not_contains "$(cat "$STUB_STATE/calls")" "gh api"
teardown

setup "one failed probe of the running version does not lower the rollback bar"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health_pre"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "passes the full health gate, so a rollback must pass it again"
assert_eq "$RC" 5 "(held to the full gate it passed on the second try) $OUT"
printf 'fail\nfail\nfail\nok {"ok":true}\n' >"$STUB_STATE/health_pre"; rm -f "$STUB_STATE/pre_count"
run_deploy DL_LIVENESS_URL=http://box/live DL_HEALTH_TIMEOUT=0 DL_TAG_PUSH=0
assert_contains "$OUT" "does not pass the health gate (3 tries)"
assert_eq "$RC" 1 "(three failures: judged by liveness) $OUT"
teardown

setup "another host must be declared private; a bad DL_TAG_PUSH is a config error"
git -C "$T/repo" remote set-url origin https://gitlab.example.com/x/y.git
run_deploy
assert_eq "$RC" 3 "$OUT"
assert_contains "$OUT" "Set DL_TAG_REMOTE_PRIVATE=1 if it is"
run_deploy DL_TAG_REMOTE_PRIVATE=1
assert_eq "$RC" 4 "(declared private; the push fails with no such host) $OUT"
run_deploy DL_TAG_PUSH=yes
assert_eq "$RC" 3 "$OUT"
assert_contains "$OUT" "DL_TAG_PUSH must be 1, local or 0"
teardown

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
