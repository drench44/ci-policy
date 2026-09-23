# shellcheck shell=bash
# deploy-lib.sh: canonical homelab docker compose deploy library.
# Canonical source: https://github.com/drench44/ci-policy/blob/main/deploy/deploy-lib.sh
DL_LIB_VERSION="1.0.0"
#
# Repos vendor this file. The first line of a vendored copy must name the
# version it was copied from, for example:
#   # vendored from drench44/ci-policy deploy/deploy-lib.sh v1.0.0 (commit abc1234)
#
# What one deploy does (dl_run):
#   1. Preflight: config is complete, tools exist, the git tree is clean.
#   2. Record the image each service is running now as <image>:pre-<sha>,
#      where <sha> is the commit being deployed. That tag is the rollback point.
#   3. Deploy: docker compose up -d --build (DL_UP_ARGS).
#   4. Poll DL_HEALTH_URL until the jq predicate DL_HEALTH_JQ is true, or time out.
#   5. Healthy: create and push git tag deploy/<service>/<UTC time>-<sha>, so
#      every rollback point also lives in git. Print the rollback command.
#   6. Not healthy: retag <image>:pre-<sha> back onto the image name compose
#      uses, recreate without building, check health again, and print the
#      exact rollback command either way.
#
# Usage (in a repo's deploy.sh):
#   source "$(dirname "$0")/deploy/deploy-lib.sh"
#   DL_SERVICE=family-hub
#   DL_REMOTE=homelab-box                   # ssh host; empty = run docker here
#   DL_COMPOSE_DIR='~/docker-services/family-hub'
#   DL_SERVICES="web"                        # empty = every service in the project
#   DL_HEALTH_URL="http://<box>:8138/health"
#   DL_HEALTH_JQ='.ok == true'
#   ...copy files to the box first (rsync), then:
#   dl_run; exit $?
#
# Exit codes of dl_run:
#   0  deployed, healthy, deploy tag pushed
#   1  deploy was unhealthy, rolled back, and the old version is healthy again
#   2  deploy was unhealthy and the rollback failed or was impossible: DOWN
#   3  config or precondition problem; nothing was changed
#   4  deployed and healthy, but the deploy tag could not be created or pushed
#
# Settings (set before calling dl_run):
#   DL_SERVICE          required. Name used in tags and messages.
#   DL_COMPOSE_DIR      required. Directory with the compose file (on DL_REMOTE if set).
#   DL_REMOTE           ssh host that runs docker. Empty = local docker.
#   DL_SERVICES         compose services to deploy and roll back. Empty = all.
#   DL_COMPOSE_ARGS     extra args after "docker compose", e.g. "-f prod.yml -p hub".
#   DL_UP_ARGS          default "up -d --build".
#   DL_HEALTH_URL       required. URL polled after the deploy.
#   DL_HEALTH_JQ        jq predicate on the response body. Default ".".
#                       Plain-text endpoints: use DL_HEALTH_JQ="" to only need HTTP 2xx.
#   DL_HEALTH_FROM      "local" (curl here, default) or "remote" (curl on DL_REMOTE,
#                       for services bound to the box's loopback only).
#   DL_HEALTH_TIMEOUT   seconds to wait for healthy. Default 120.
#   DL_HEALTH_INTERVAL  seconds between polls. Default 5.
#   DL_GIT_DIR          repo being deployed. Default: current directory.
#   DL_SHA              commit being deployed. Default: HEAD of DL_GIT_DIR.
#   DL_GIT_REMOTE       where the deploy tag is pushed. Default origin.
#   DL_TAG_PUSH         1 = create and push the deploy tag (default), 0 = skip.
#   DL_ALLOW_DIRTY      1 = allow deploying with uncommitted changes. Default 0.

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

dl_preflight() {
  local missing=()
  [[ -n "${DL_SERVICE:-}" ]] || missing+=(DL_SERVICE)
  [[ -n "${DL_COMPOSE_DIR:-}" ]] || missing+=(DL_COMPOSE_DIR)
  [[ -n "${DL_HEALTH_URL:-}" ]] || missing+=(DL_HEALTH_URL)
  if (( ${#missing[@]} )); then
    dl_err "missing settings: ${missing[*]}"
    return 3
  fi
  case "${DL_HEALTH_FROM:-local}" in
    local|remote) ;;
    *) dl_err "DL_HEALTH_FROM must be local or remote, not '${DL_HEALTH_FROM}'"; return 3 ;;
  esac
  if [[ "${DL_HEALTH_FROM:-local}" == remote && -z "${DL_REMOTE:-}" ]]; then
    dl_err "DL_HEALTH_FROM=remote needs DL_REMOTE"; return 3
  fi
  local tool
  for tool in curl jq git; do
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
  return 0
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
  if [[ -n "${DL_HEALTH_JQ-.}" ]]; then
    if ! printf '%s' "$body" | jq -e "${DL_HEALTH_JQ-.}" >/dev/null 2>&1; then
      printf 'predicate %s was not true for: %s' "${DL_HEALTH_JQ-.}" "${body:0:200}"
      return 1
    fi
  fi
  return 0
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
  [[ -n "${DL_PRE:-}" ]] || { echo "(no rollback point was recorded)"; return 0; }
  local svc repo tag cmds="" svcs=""
  while read -r svc repo tag; do
    [[ -n "$svc" ]] || continue
    cmds+="docker tag $repo:pre-$DL_SHORT_SHA $repo:$tag && "
    svcs+=" $svc"
  done <<<"$DL_PRE"
  local compose_args=""
  [[ -n "${DL_COMPOSE_ARGS:-}" ]] && compose_args=" $DL_COMPOSE_ARGS"
  local inner="cd $DL_COMPOSE_DIR && ${cmds}docker compose${compose_args} up -d --no-build --force-recreate${svcs}"
  if [[ -n "${DL_REMOTE:-}" ]]; then
    printf "ssh %s '%s'\n" "$DL_REMOTE" "$inner"
  else
    printf '%s\n' "$inner"
  fi
}

# Put the recorded images back and recreate those services without building.
dl_rollback() {
  [[ -n "${DL_PRE:-}" ]] || { dl_err "no rollback point was recorded"; return 1; }
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

# Create and push deploy/<service>/<UTC time>-<sha>.
dl_tag_deploy() {
  local gitdir="${DL_GIT_DIR:-.}" stamp tag
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  tag="deploy/$DL_SERVICE/$stamp-$DL_SHORT_SHA"
  git -C "$gitdir" tag -a "$tag" "$DL_FULL_SHA" -m "Deployed $DL_SERVICE at $DL_SHORT_SHA ($stamp)" \
    || { dl_err "could not create git tag $tag"; return 1; }
  # shellcheck disable=SC2034  # read by the caller after dl_run
  DL_DEPLOY_TAG="$tag"
  git -C "$gitdir" push "${DL_GIT_REMOTE:-origin}" "refs/tags/$tag" \
    || { dl_err "created $tag but could not push it; run: git push ${DL_GIT_REMOTE:-origin} refs/tags/$tag"; return 1; }
  dl_log "recorded deploy tag $tag"
  return 0
}

dl_run() {
  local rc
  dl_preflight || return 3
  local services
  services=$(dl_services) || { dl_err "could not list compose services"; return 3; }
  dl_log "deploying $DL_SHORT_SHA (services: ${services//$'\n'/ }) with deploy-lib $DL_LIB_VERSION"
  dl_record_pre || { dl_err "stopping before deploy: could not record rollback points"; return 3; }

  # shellcheck disable=SC2086  # DL_UP_ARGS and DL_SERVICES are word lists on purpose
  if dl_compose ${DL_UP_ARGS:-up -d --build} ${DL_SERVICES:-}; then
    if dl_wait_healthy; then
      rc=0
      if [[ "${DL_TAG_PUSH:-1}" == 1 ]]; then
        dl_tag_deploy || rc=4
      fi
      dl_log "deploy OK. To roll back to what ran before:"
      dl_rollback_command >&2
      return "$rc"
    fi
  else
    dl_err "docker compose up failed"
  fi

  if [[ -z "${DL_PRE:-}" ]]; then
    dl_err "no rollback point was recorded, so nothing can be rolled back. SERVICE IS DOWN."
    return 2
  fi
  dl_warn "rolling back to the images recorded before this deploy"
  if dl_rollback && dl_wait_healthy; then
    dl_err "deploy of $DL_SHORT_SHA failed; rolled back and the old version is healthy"
    dl_log "the rollback ran:"
    dl_rollback_command >&2
    return 1
  fi
  dl_err "ROLLBACK FAILED. SERVICE IS DOWN. Run this by hand, then check $DL_HEALTH_URL:"
  dl_rollback_command >&2
  return 2
}
