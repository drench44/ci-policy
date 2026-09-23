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
    DL_STATE_DIR="$T/boxstate" DL_HEALTH_SETTLE=0
    for kv in "$@"; do eval "$kv"; done
    dl_run
    echo "RC=$?"
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
assert_contains "$OUT" "rolled back and the old version is healthy"
teardown

setup "predicate false counts as unhealthy"
echo 'ok {"ok":false}' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2
assert_contains "$OUT" "DL_HEALTH_JQ predicate is not true"
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
assert_eq "$RC" 2
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
assert_eq "$RC" 2 "(stale everywhere, rollback also stale)"
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

setup "if the files cannot be restored the images are still rolled back, and it says so"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy "dl_sync() { rm -f '$T'/boxstate/snapshot-*.tgz; }" DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 1 "$OUT"
assert_contains "$OUT" "compose directory was not restored"
assert_contains "$CALLS" "--no-build --force-recreate web"
teardown

setup "a service with no rollback point that stays on the new build makes it exit 2"
printf 'web\nworker\n' >"$STUB_STATE/services"; echo hub-worker >"$STUB_STATE/images/worker"
printf 'fail\nok {"ok":true}\n' >"$STUB_STATE/health"
run_deploy DL_HEALTH_TIMEOUT=0
assert_eq "$RC" 2 "$OUT"
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

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
