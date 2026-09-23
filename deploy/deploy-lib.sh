# shellcheck shell=bash
# deploy-lib.sh: shared homelab docker compose deploy with a real health gate
# and automatic rollback. Canonical source: drench44/ci-policy deploy/deploy-lib.sh
DL_LIB_VERSION="2.0.0"
#
# Most repos should not source this directly: write a small config file and
# run deploy/homelab-deploy <config> (see deploy/example.deploy.conf).
#
# Why it exists: a health endpoint that always says "ok" hides a broken
# deploy. On 2026-09-17 a deploy deleted a box's .env, the app still answered
# /health with ok, and a card silently vanished for a day. So this library
# refuses to run with a shallow health check: the config must name fields the
# health response has to carry (DL_HEALTH_REQUIRE) and/or a timestamp that
# must be recent (DL_HEALTH_FRESH), and it can require files and env values on
# the box before anything restarts (DL_REQUIRE_FILES, DL_REQUIRE_ENV).
#
# What one deploy does (dl_run):
#   1. Preflight: settings complete, tools present, git tree clean, health
#      gate not shallow.
#   2. Rollback point: tag each service's running image <image>:pre-<sha>
#      (<sha> = the commit being deployed), snapshot the compose directory
#      to a tarball on the box, and create + push git tag
#      rollback-point/<service>/<UTC time> on the commit that was running
#      (read from the box's state file). Git now records what to go back to.
#   3. dl_sync, if the config defines it (for example rsync the repo to the box).
#   4. Required files and env values on the box. Missing: restore the
#      snapshot and stop before anything restarts.
#   5. docker compose up -d --build.
#   6. Poll the health URL until every requirement holds, or time out.
#   7. Healthy: record the new commit in the box state file, create + push
#      git tag deploy/<service>/<UTC time>-<sha>.
#      Not healthy: restore the snapshot, retag <image>:pre-<sha> back onto
#      the compose image name, recreate without building, check health
#      again, and create + push git tag failed-deploy/<service>/<time>-<sha>.
#
# Exit codes of dl_run:
#   0  deployed, healthy, tags pushed
#   1  deploy was unhealthy (or dl_sync failed), rolled back, old version healthy
#   2  deploy was unhealthy and the rollback failed or was impossible: DOWN
#   3  config or precondition problem; nothing was restarted (box files restored)
#   4  deployed and healthy, but a git tag could not be created or pushed
#
# Settings:
#   DL_SERVICE          required. Name used in tags, state and messages.
#   DL_COMPOSE_DIR      required. Directory with the compose file (on DL_REMOTE if set).
#   DL_REMOTE           ssh host that runs docker. Empty = local docker.
#   DL_SERVICES         compose services to deploy and roll back. Empty = all.
#   DL_COMPOSE_ARGS     extra args after "docker compose", e.g. "-f prod.yml -p hub".
#   DL_UP_ARGS          default "up -d --build".
#   DL_HEALTH_URL       required. URL polled after the deploy.
#   DL_HEALTH_REQUIRE   jq paths (space or newline separated) that must be present
#                       and truthy in the JSON response, e.g.
#                       ".integrations.home_assistant.ok .laundry.washer".
#   DL_HEALTH_FRESH     "<jq path> <max age seconds>" lines: a timestamp (ISO 8601
#                       with Z or an offset, or epoch seconds/ms) that must be at
#                       most that old, e.g. ".weather.observed_at 5400".
#   DL_HEALTH_JQ        optional extra jq predicate that must be true.
#   DL_HEALTH_SHALLOW_OK=1  allow a gate with none of the three above (not advised).
#   DL_HEALTH_FROM      "local" (curl here, default) or "remote" (curl on DL_REMOTE,
#                       for services bound to the box's loopback only).
#   DL_HEALTH_TIMEOUT   seconds to wait for healthy. Default 120.
#   DL_HEALTH_INTERVAL  seconds between polls. Default 5.
#   DL_REQUIRE_FILES    files (relative to DL_COMPOSE_DIR) that must exist and be
#                       non-empty on the box before restarting, e.g. ".env config.json".
#   DL_REQUIRE_ENV      "file:VAR" items: VAR must have a non-empty value in that
#                       env file on the box, e.g. ".env:HA_TOKEN".
#   DL_SNAPSHOT         1 (default) = tar the compose dir before dl_sync and restore
#                       it on rollback. Files dl_sync ADDS are left in place.
#   DL_SNAPSHOT_EXCLUDE paths (tar --exclude patterns) left out of the snapshot,
#                       e.g. "./data ./photos". Keep big data out.
#   DL_SNAPSHOT_KEEP    snapshots kept per service. Default 5.
#   DL_STATE_DIR        state dir on the box. Default
#                       $HOME/.local/state/homelab-deploy/<service> (box's $HOME).
#   DL_GIT_DIR          repo being deployed. Default: current directory.
#   DL_SHA              commit being deployed. Default: HEAD of DL_GIT_DIR.
#   DL_GIT_REMOTE       where tags are pushed. Default origin.
#   DL_TAG_PUSH         1 = create and push git tags (default), 0 = skip.
#   DL_ALLOW_DIRTY      1 = allow deploying with uncommitted changes. Default 0.
# Hook:
#   dl_sync             optional shell function, run after the rollback point is
#                       recorded and before the restart. Non-zero = roll back files.

dl_log()  { printf '[deploy %s] %s\n' "${DL_SERVICE:-?}" "$*" >&2; }
dl_warn() { printf '[deploy %s] WARNING: %s\n' "${DL_SERVICE:-?}" "$*" >&2; }
dl_err()  { printf '[deploy %s] ERROR: %s\n' "${DL_SERVICE:-?}" "$*" >&2; }

# Quote a directory for a remote shell, keeping a leading ~ expandable.
# shellcheck disable=SC2088  # the ~ is printed for the remote shell to expand
_dl_remote_dir() {
  local dir="$1"
  if [[ "$dir" == "~" ]]; then
    printf '~'
  elif [[ "$dir" == "~/"* ]]; then
    printf '~/%q' "${dir#\~/}"
  else
    printf '%q' "$dir"
  fi
}

# Run a bash snippet inside DL_COMPOSE_DIR, locally or over ssh. Extra
# arguments become $1.. of the snippet.
dl_sh() {
  local script="$1"; shift
  if [[ -n "${DL_REMOTE:-}" ]]; then
    local args=""
    (($#)) && args=$(printf ' %q' "$@")
    ssh -o BatchMode=yes "$DL_REMOTE" \
      "cd $(_dl_remote_dir "$DL_COMPOSE_DIR") && bash -c $(printf '%q' "$script") dl-sh$args"
  else
    local dir="${DL_COMPOSE_DIR/#\~/$HOME}"
    (cd "$dir" && bash -c "$script" dl-sh "$@")
  fi
}

# Run "docker <args>" in DL_COMPOSE_DIR, locally or over ssh.
dl_docker() {
  if [[ -n "${DL_REMOTE:-}" ]]; then
    local quoted
    quoted=$(printf '%q ' "$@")
    ssh -o BatchMode=yes "$DL_REMOTE" "cd $(_dl_remote_dir "$DL_COMPOSE_DIR") && docker $quoted"
  else
    local dir="${DL_COMPOSE_DIR/#\~/$HOME}"
    (cd "$dir" && docker "$@")
  fi
}

dl_compose() {
  # shellcheck disable=SC2086  # DL_COMPOSE_ARGS is a word list on purpose
  dl_docker compose ${DL_COMPOSE_ARGS:-} "$@"
}

# The services this deploy touches, one per line.
dl_services() {
  if [[ -n "${DL_SERVICES:-}" ]]; then
    # shellcheck disable=SC2086  # a space-separated list on purpose
    printf '%s\n' $DL_SERVICES
  else
    dl_compose config --services
  fi
}

# Split an image reference into repo and tag ("latest" when none).
# A colon only starts a tag when no slash follows it (registry:5000/x has no tag).
_dl_split_image() {
  local ref="$1" repo tag
  ref="${ref%%@*}"
  if [[ "${ref##*/}" == *:* ]]; then
    repo="${ref%:*}"
    tag="${ref##*:}"
  else
    repo="$ref"
    tag="latest"
  fi
  printf '%s %s\n' "$repo" "$tag"
}

# The image name compose runs for a service (from the compose config).
dl_image_for() {
  local svc="$1" out
  out=$(dl_compose config --images "$svc") || return 1
  out=$(printf '%s\n' "$out" | head -n 1)
  [[ -n "$out" ]] || return 1
  printf '%s\n' "$out"
}

# Image id of the service's running container, empty when none is running.
dl_running_image_id() {
  local svc="$1" cid
  cid=$(dl_compose ps -q "$svc" | head -n 1) || return 1
  [[ -n "$cid" ]] || return 0
  dl_docker inspect --format '{{.Image}}' "$cid"
}

_dl_trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; printf '%s' "${s%"${s##*[![:space:]]}"}"; }

dl_preflight() {
  local missing=()
  [[ -n "${DL_SERVICE:-}" ]] || missing+=(DL_SERVICE)
  [[ -n "${DL_COMPOSE_DIR:-}" ]] || missing+=(DL_COMPOSE_DIR)
  [[ -n "${DL_HEALTH_URL:-}" ]] || missing+=(DL_HEALTH_URL)
  if (( ${#missing[@]} )); then
    dl_err "missing settings: ${missing[*]}"
    return 3
  fi
  if [[ ! "$DL_SERVICE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    dl_err "DL_SERVICE '$DL_SERVICE' must be letters, digits, dot, dash or underscore"
    return 3
  fi
  case "${DL_HEALTH_FROM:-local}" in
    local|remote) ;;
    *) dl_err "DL_HEALTH_FROM must be local or remote, not '${DL_HEALTH_FROM}'"; return 3 ;;
  esac
  if [[ "${DL_HEALTH_FROM:-local}" == remote && -z "${DL_REMOTE:-}" ]]; then
    dl_err "DL_HEALTH_FROM=remote needs DL_REMOTE"; return 3
  fi
  local jq_extra
  jq_extra=$(_dl_trim "${DL_HEALTH_JQ:-}")
  if [[ -z "$(_dl_trim "${DL_HEALTH_REQUIRE:-}")" && -z "$(_dl_trim "${DL_HEALTH_FRESH:-}")" ]] \
     && [[ -z "$jq_extra" || "$jq_extra" == "." || "$jq_extra" == "true" ]] \
     && [[ "${DL_HEALTH_SHALLOW_OK:-0}" != 1 ]]; then
    dl_err "the health gate is shallow: set DL_HEALTH_REQUIRE (fields that prove the app works, such as a configured integration) and/or DL_HEALTH_FRESH (a data timestamp that must be recent). An endpoint that only says ok is how a deploy once deleted a box's .env unnoticed."
    return 3
  fi
  local line path age
  while IFS= read -r line; do
    line=$(_dl_trim "$line"); [[ -n "$line" ]] || continue
    read -r path age <<<"$line"
    if [[ -z "$path" || ! "$age" =~ ^[0-9]+$ ]]; then
      dl_err "DL_HEALTH_FRESH line '$line' must be '<jq path> <max age seconds>'"; return 3
    fi
  done <<<"${DL_HEALTH_FRESH:-}"
  local tool
  for tool in curl jq git tar; do
    command -v "$tool" >/dev/null 2>&1 || { dl_err "$tool is not installed"; return 3; }
  done
  [[ -n "${DL_REMOTE:-}" ]] || command -v docker >/dev/null 2>&1 \
    || { dl_err "docker is not installed (set DL_REMOTE to deploy over ssh)"; return 3; }
  local gitdir="${DL_GIT_DIR:-.}"
  if ! git -C "$gitdir" rev-parse --git-dir >/dev/null 2>&1; then
    dl_err "DL_GIT_DIR '$gitdir' is not a git repository"; return 3
  fi
  if [[ "${DL_ALLOW_DIRTY:-0}" != 1 ]] && [[ -n "$(git -C "$gitdir" status --porcelain)" ]]; then
    dl_err "the working tree has uncommitted changes; commit them first (or DL_ALLOW_DIRTY=1)"
    return 3
  fi
  DL_FULL_SHA=$(git -C "$gitdir" rev-parse --verify "${DL_SHA:-HEAD}^{commit}" 2>/dev/null) \
    || { dl_err "cannot resolve commit '${DL_SHA:-HEAD}'"; return 3; }
  DL_SHORT_SHA="${DL_FULL_SHA:0:12}"
  DL_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  return 0
}

# The state dir expression, expanded by the shell that runs dl_sh.
_dl_state_dir() {
  if [[ -n "${DL_STATE_DIR:-}" ]]; then
    printf '%s' "$DL_STATE_DIR"
  else
    # shellcheck disable=SC2016  # $HOME is expanded on the box, not here
    printf '%s' '$HOME/.local/state/homelab-deploy/'"$DL_SERVICE"
  fi
}

# The commit the box says is deployed now (empty when unknown).
dl_read_deployed_sha() {
  dl_sh "f=\"$(_dl_state_dir)/deployed-sha\"; [ -f \"\$f\" ] && cat \"\$f\" || true" \
    2>/dev/null | head -n 1 | tr -cd '0-9a-f'
}

dl_write_deployed_sha() {
  dl_sh "d=\"$(_dl_state_dir)\"; mkdir -p \"\$d\" && printf '%s\n' \"\$1\" >\"\$d/deployed-sha.tmp\" && mv \"\$d/deployed-sha.tmp\" \"\$d/deployed-sha\"" \
    "$DL_FULL_SHA"
}

# Record every service's running image as <repo>:pre-<sha>.
# Sets DL_PRE (lines of "service repo tag") for dl_rollback.
dl_record_pre() {
  DL_PRE=""
  local svc image id repo tag services
  services=$(dl_services) || { dl_err "could not list compose services"; return 1; }
  for svc in $services; do
    image=$(dl_image_for "$svc") || { dl_err "could not read the image name for $svc"; return 1; }
    read -r repo tag <<<"$(_dl_split_image "$image")"
    if dl_docker image inspect --format '{{.Id}}' "$repo:pre-$DL_SHORT_SHA" >/dev/null 2>&1; then
      # An earlier run of this same commit already recorded what ran before
      # it. Overwriting would replace that with this commit's own image.
      dl_log "rollback point: $svc keeps $repo:pre-$DL_SHORT_SHA from an earlier run"
      DL_PRE+="$svc $repo $tag"$'\n'
      continue
    fi
    id=$(dl_running_image_id "$svc") || { dl_err "could not inspect $svc"; return 1; }
    if [[ -z "$id" ]]; then
      dl_warn "$svc is not running, so it has no rollback point"
      continue
    fi
    dl_docker tag "$id" "$repo:pre-$DL_SHORT_SHA" \
      || { dl_err "could not tag $svc's running image as $repo:pre-$DL_SHORT_SHA"; return 1; }
    dl_log "rollback point: $svc runs $repo:pre-$DL_SHORT_SHA"
    DL_PRE+="$svc $repo $tag"$'\n'
  done
  return 0
}

# Tar the compose dir into the state dir. Sets DL_SNAPSHOT_FILE.
dl_snapshot() {
  DL_SNAPSHOT_FILE=""
  [[ "${DL_SNAPSHOT:-1}" == 1 ]] || return 0
  local excludes=() p
  # shellcheck disable=SC2086  # a word list on purpose
  for p in ${DL_SNAPSHOT_EXCLUDE:-}; do excludes+=("--exclude=$p"); done
  local name="snapshot-$DL_STAMP-$DL_SHORT_SHA.tgz"
  # $1 = file name, $2 = how many to keep, the rest = tar excludes.
  local script
  script="d=\"$(_dl_state_dir)\"; mkdir -p \"\$d\" || exit 1
f=\"\$1\" keep=\"\$2\"; shift 2
tar -czf \"\$d/\$f.tmp\" \"\$@\" --exclude=./.git . || { rm -f \"\$d/\$f.tmp\"; exit 1; }
mv \"\$d/\$f.tmp\" \"\$d/\$f\" || exit 1
ls -1t \"\$d\"/snapshot-*.tgz 2>/dev/null | tail -n +\$((keep + 1)) | while IFS= read -r old; do rm -f \"\$old\"; done
exit 0"
  if ! dl_sh "$script" "$name" "${DL_SNAPSHOT_KEEP:-5}" ${excludes[@]+"${excludes[@]}"}; then
    dl_err "could not snapshot $DL_COMPOSE_DIR"
    return 1
  fi
  DL_SNAPSHOT_FILE="$name"
  dl_log "rollback point: files snapshot $(_dl_state_dir)/$name"
  return 0
}

dl_restore_snapshot() {
  [[ -n "${DL_SNAPSHOT_FILE:-}" ]] || return 0
  if dl_sh "tar -xzf \"$(_dl_state_dir)/\$1\" -C ." "$DL_SNAPSHOT_FILE"; then
    dl_log "restored the compose directory from $DL_SNAPSHOT_FILE"
    return 0
  fi
  dl_err "could not restore the compose directory from $DL_SNAPSHOT_FILE"
  return 1
}

# Every DL_REQUIRE_FILES file exists and is non-empty; every DL_REQUIRE_ENV
# variable has a non-empty value. Prints each problem.
dl_check_required() {
  [[ -n "$(_dl_trim "${DL_REQUIRE_FILES:-}${DL_REQUIRE_ENV:-}")" ]] || return 0
  local script
  script=$(cat <<'SCRIPT'
bad=0
for f in $1; do
  [ -s "$f" ] || { echo "missing or empty: $f"; bad=1; }
done
for item in $2; do
  f=${item%%:*}; v=${item#*:}
  if [ ! -s "$f" ]; then echo "missing or empty: $f (needed for $v)"; bad=1; continue; fi
  val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?$v=" "$f" | tail -n 1 | sed -E 's/^[^=]*=//' | tr -d "\"' \t\r")
  [ -n "$val" ] || { echo "$f has no value for $v"; bad=1; }
done
exit $bad
SCRIPT
)
  local out
  if out=$(dl_sh "$script" "${DL_REQUIRE_FILES:-}" "${DL_REQUIRE_ENV:-}" 2>&1); then
    return 0
  fi
  local line
  while IFS= read -r line; do [[ -n "$line" ]] && dl_err "required on the box: $line"; done <<<"$out"
  return 1
}

# jq program: true when every requirement holds; otherwise prints the first
# failure as a string and exits false.
_dl_health_program() {
  # shellcheck disable=SC2016  # a jq program; its $vars are jq variables
  local prog='def epoch:
  if type == "number" then (if . > 100000000000 then . / 1000 else . end)
  elif type == "string" then
    (sub("\\.[0-9]+"; "")) as $s
    | if ($s | test("Z$")) then ($s | fromdateiso8601)
      elif ($s | test("[+-][0-9]{2}:?[0-9]{2}$")) then
        ($s[0:19] + "Z" | fromdateiso8601)
        - (($s | capture("(?<sign>[+-])(?<h>[0-9]{2}):?(?<m>[0-9]{2})$")) as $o
           | (($o.h | tonumber) * 3600 + ($o.m | tonumber) * 60)
             * (if $o.sign == "+" then 1 else -1 end))
      else ($s[0:19] + "Z" | fromdateiso8601) end
  else null end;
[ ' first=1 p age
  local path
  # shellcheck disable=SC2086  # a word list on purpose
  for path in ${DL_HEALTH_REQUIRE:-}; do
    [[ $first == 1 ]] || prog+=', '
    first=0
    prog+="(try ($path) catch null) as \$v | if (\$v == null or \$v == false or \$v == \"\") then \"required $path is missing or false\" else empty end"
  done
  local line
  while IFS= read -r line; do
    line=$(_dl_trim "$line"); [[ -n "$line" ]] || continue
    read -r p age <<<"$line"
    [[ $first == 1 ]] || prog+=', '
    first=0
    prog+="((try ($p) catch null) as \$t | (try (\$t | epoch) catch null) as \$e | if \$e == null then \"fresh $p is missing or not a timestamp\" elif (now - \$e) > $age then \"fresh $p is \\(((now - \$e) / 60 | floor)) min old (limit $age s)\" else empty end)"
  done <<<"${DL_HEALTH_FRESH:-}"
  if [[ -n "$(_dl_trim "${DL_HEALTH_JQ:-}")" ]]; then
    [[ $first == 1 ]] || prog+=', '
    prog+="(if (try (${DL_HEALTH_JQ}) catch false) then empty else \"DL_HEALTH_JQ predicate is not true\" end)"
  fi
  prog+=' ] | if length == 0 then true else (.[0] | halt_error(1)) end'
  printf '%s' "$prog"
}

# One health probe. Prints the reason on failure.
dl_health_once() {
  local body
  if [[ "${DL_HEALTH_FROM:-local}" == remote ]]; then
    body=$(ssh -o BatchMode=yes "$DL_REMOTE" "curl -fsS --max-time 10 $(printf '%q' "$DL_HEALTH_URL")" 2>&1) \
      || { printf 'request failed: %s' "${body:0:200}"; return 1; }
  else
    body=$(curl -fsS --max-time 10 "$DL_HEALTH_URL" 2>&1) \
      || { printf 'request failed: %s' "${body:0:200}"; return 1; }
  fi
  if ! printf '%s' "$body" | jq -e . >/dev/null 2>&1; then
    printf 'response is not JSON: %s' "${body:0:200}"
    return 1
  fi
  local why
  if why=$(printf '%s' "$body" | jq -e "$(_dl_health_program)" 2>&1 >/dev/null); then
    return 0
  fi
  printf '%s' "${why:-health program failed}"
  return 1
}

# Poll until healthy or DL_HEALTH_TIMEOUT seconds pass.
dl_wait_healthy() {
  local timeout="${DL_HEALTH_TIMEOUT:-120}" interval="${DL_HEALTH_INTERVAL:-5}"
  local start=$SECONDS reason tries=0
  while :; do
    tries=$((tries + 1))
    if reason=$(dl_health_once); then
      dl_log "healthy after $tries check(s): $DL_HEALTH_URL"
      return 0
    fi
    if (( SECONDS - start >= timeout )); then
      dl_err "not healthy after ${timeout}s ($tries checks). Last: $reason"
      return 1
    fi
    sleep "$interval"
  done
}

# The exact commands that put the recorded images back.
dl_rollback_command() {
  [[ -n "${DL_PRE:-}" ]] || { echo "(no image rollback point was recorded)"; return 0; }
  local svc repo tag cmds="" svcs=""
  while read -r svc repo tag; do
    [[ -n "$svc" ]] || continue
    cmds+="docker tag $repo:pre-$DL_SHORT_SHA $repo:$tag && "
    svcs+=" $svc"
  done <<<"$DL_PRE"
  local compose_args="" restore=""
  [[ -n "${DL_COMPOSE_ARGS:-}" ]] && compose_args=" $DL_COMPOSE_ARGS"
  [[ -n "${DL_SNAPSHOT_FILE:-}" ]] && restore="tar -xzf $(_dl_state_dir)/$DL_SNAPSHOT_FILE -C . && "
  local inner="cd $DL_COMPOSE_DIR && ${restore}${cmds}docker compose${compose_args} up -d --no-build --force-recreate${svcs}"
  if [[ -n "${DL_REMOTE:-}" ]]; then
    printf "ssh %s '%s'\n" "$DL_REMOTE" "$inner"
  else
    printf '%s\n' "$inner"
  fi
}

# Put the recorded images back and recreate those services without building.
dl_rollback() {
  [[ -n "${DL_PRE:-}" ]] || { dl_err "no image rollback point was recorded"; return 1; }
  local svc repo tag svcs=()
  while read -r svc repo tag; do
    [[ -n "$svc" ]] || continue
    dl_docker tag "$repo:pre-$DL_SHORT_SHA" "$repo:$tag" \
      || { dl_err "could not retag $repo:pre-$DL_SHORT_SHA"; return 1; }
    svcs+=("$svc")
  done <<<"$DL_PRE"
  dl_compose up -d --no-build --force-recreate "${svcs[@]}" \
    || { dl_err "docker compose up during rollback failed"; return 1; }
  return 0
}

# Create and push an annotated tag. Usage: dl_git_tag <tag> <commit> <message>
dl_git_tag() {
  [[ "${DL_TAG_PUSH:-1}" == 1 ]] || return 0
  local gitdir="${DL_GIT_DIR:-.}" tag="$1" commit="$2" msg="$3"
  git -C "$gitdir" tag -a "$tag" "$commit" -m "$msg" \
    || { dl_err "could not create git tag $tag"; return 1; }
  git -C "$gitdir" push -q "${DL_GIT_REMOTE:-origin}" "refs/tags/$tag" \
    || { dl_err "created $tag but could not push it; run: git push ${DL_GIT_REMOTE:-origin} refs/tags/$tag"; return 1; }
  dl_log "git tag $tag"
  return 0
}

# Tag the commit the box was running before this deploy, so git records the
# rollback point. Unknown (first deploy with this library) is a warning.
dl_tag_rollback_point() {
  [[ "${DL_TAG_PUSH:-1}" == 1 ]] || return 0
  local prev gitdir="${DL_GIT_DIR:-.}"
  prev=$(dl_read_deployed_sha)
  if [[ -z "$prev" ]]; then
    dl_warn "the box does not record which commit it runs yet (first deploy with this library); only the image and file snapshot mark the rollback point"
    return 0
  fi
  if ! git -C "$gitdir" cat-file -e "$prev^{commit}" 2>/dev/null; then
    dl_warn "the box says it runs $prev, which this clone does not have; not tagging it"
    return 0
  fi
  dl_git_tag "rollback-point/$DL_SERVICE/$DL_STAMP" "$prev" \
    "Running on $DL_SERVICE before deploying $DL_SHORT_SHA at $DL_STAMP. Images: <image>:pre-$DL_SHORT_SHA. Files: ${DL_SNAPSHOT_FILE:-none}."
}

# Undo files after a failure before anything restarted.
_dl_abort_before_restart() {
  dl_restore_snapshot || dl_err "the compose directory may be half-synced; restore it by hand"
}

dl_run() {
  local rc=0 tag_rc=0
  dl_preflight || return 3
  local services
  services=$(dl_services) || { dl_err "could not list compose services"; return 3; }
  dl_log "deploying $DL_SHORT_SHA (services: ${services//$'\n'/ }) with deploy-lib $DL_LIB_VERSION"
  dl_record_pre || { dl_err "stopping before deploy: could not record rollback points"; return 3; }
  dl_snapshot || { dl_err "stopping before deploy: could not snapshot the compose directory"; return 3; }
  dl_tag_rollback_point || tag_rc=4

  if declare -F dl_sync >/dev/null; then
    dl_log "running dl_sync"
    if ! dl_sync; then
      dl_err "dl_sync failed; putting the files back, nothing was restarted"
      _dl_abort_before_restart
      return 1
    fi
  fi
  if ! dl_check_required; then
    dl_err "refusing to restart with required files or settings missing; putting the files back"
    _dl_abort_before_restart
    return 3
  fi

  # shellcheck disable=SC2086  # DL_UP_ARGS and DL_SERVICES are word lists on purpose
  if dl_compose ${DL_UP_ARGS:-up -d --build} ${DL_SERVICES:-}; then
    if dl_wait_healthy; then
      dl_write_deployed_sha || { dl_warn "could not record the deployed commit on the box"; tag_rc=4; }
      dl_git_tag "deploy/$DL_SERVICE/$DL_STAMP-$DL_SHORT_SHA" "$DL_FULL_SHA" \
        "Deployed $DL_SERVICE at $DL_SHORT_SHA ($DL_STAMP), healthy." || tag_rc=4
      dl_log "deploy OK. To roll back to what ran before:"
      dl_rollback_command >&2
      return "$tag_rc"
    fi
  else
    dl_err "docker compose up failed"
  fi

  dl_git_tag "failed-deploy/$DL_SERVICE/$DL_STAMP-$DL_SHORT_SHA" "$DL_FULL_SHA" \
    "Deploy of $DL_SERVICE at $DL_SHORT_SHA ($DL_STAMP) was unhealthy and rolled back." || true
  if [[ -z "${DL_PRE:-}" ]]; then
    dl_restore_snapshot || true
    dl_err "no image rollback point was recorded, so nothing can be rolled back. SERVICE IS DOWN."
    return 2
  fi
  dl_warn "rolling back to the files and images recorded before this deploy"
  rc=0
  dl_restore_snapshot || rc=1
  if [[ $rc == 0 ]] && dl_rollback && dl_wait_healthy; then
    dl_err "deploy of $DL_SHORT_SHA failed; rolled back and the old version is healthy"
    dl_log "the rollback ran:"
    dl_rollback_command >&2
    return 1
  fi
  dl_err "ROLLBACK FAILED. SERVICE IS DOWN. Run this by hand, then check $DL_HEALTH_URL:"
  dl_rollback_command >&2
  return 2
}
