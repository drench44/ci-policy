#!/usr/bin/env bash
# Install, check, or remove the ci-policy global git hooks on this machine.
#
#   install/install-git-hooks.sh                 install for every repo (global core.hooksPath)
#   install/install-git-hooks.sh --scope ~/workspace
#                                                install only for repos under ~/workspace
#                                                (git includeIf), so automation clones
#                                                elsewhere on the box are untouched
#   install/install-git-hooks.sh --check         report; exit 1 if anything is off
#   install/install-git-hooks.sh --uninstall     undo everything, restoring repo hooks
#
# What install does:
#   1. Copies lib/ci_policy, policy/main-allowlist.json and git-hooks/dispatch
#      to --prefix (default ~/.config/ci-policy), plus bin/ci-policy.
#   2. Writes one small wrapper per git hook name into --hooks-dir (default
#      ~/.config/git/hooks). Each wrapper runs the dispatcher, which applies the
#      policy (pre-commit, pre-push) and then chains to the repo's own hook.
#   3. Points git at that directory: core.hooksPath in ~/.gitconfig, or with
#      --scope, an includeIf per scope directory.
#   4. Repos under the scan roots whose OWN config sets core.hooksPath (for
#      example .githooks) would shadow the global setting and skip the policy.
#      Install moves that value to ci-policy.chainHooksPath, so the dispatcher
#      still runs those hooks after the policy. --uninstall moves it back.
#
# Options:
#   --prefix DIR      where the code goes (default ~/.config/ci-policy)
#   --hooks-dir DIR   the global hooks directory (default ~/.config/git/hooks)
#   --scope DIR       repeatable; scoped install (see above)
#   --scan DIR        repeatable; roots searched for repos to migrate. Default:
#                     the scopes, else ~/Documents, ~/.claude and ~/workspace.
#   --force           replace a core.hooksPath or hooks directory not made by ci-policy
#   --dry-run         print what would change, change nothing
set -euo pipefail

HOOK_NAMES=(applypatch-msg pre-applypatch post-applypatch pre-commit pre-merge-commit
  prepare-commit-msg commit-msg post-commit pre-rebase post-checkout post-merge pre-push
  post-rewrite reference-transaction pre-auto-gc push-to-checkout sendemail-validate
  post-index-change)
MARK="ci-policy managed"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PREFIX="$HOME/.config/ci-policy"
HOOKS_DIR="$HOME/.config/git/hooks"
SCOPES=()
SCANS=()
MODE=install
FORCE=0
DRY=0

die() { echo "install-git-hooks: $*" >&2; exit 1; }
say() { echo "install-git-hooks: $*"; }
run() { if [[ $DRY == 1 ]]; then echo "  (dry run) $*"; else "$@"; fi; }

abspath() {
  local p="$1"
  # shellcheck disable=SC2088  # matching a literal "~/" prefix on purpose
  case "$p" in "~") p="$HOME" ;; "~/"*) p="$HOME/${p#\~/}" ;; esac
  [[ "$p" == /* ]] || p="$PWD/$p"
  printf '%s\n' "${p%/}"
}

while (($#)); do
  case "$1" in
    --prefix) PREFIX=$(abspath "${2:?}"); shift 2 ;;
    --hooks-dir) HOOKS_DIR=$(abspath "${2:?}"); shift 2 ;;
    --scope) SCOPES+=("$(abspath "${2:?}")"); shift 2 ;;
    --scan) SCANS+=("$(abspath "${2:?}")"); shift 2 ;;
    --check) MODE=check; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done

if ((${#SCANS[@]} == 0)); then
  if ((${#SCOPES[@]})); then
    SCANS=("${SCOPES[@]}")
  else
    for d in "$HOME/Documents" "$HOME/.claude" "$HOME/workspace"; do
      [[ -d "$d" ]] && SCANS+=("$d")
    done
  fi
fi

INCLUDE_FILE="$PREFIX/gitconfig"

# Every git repo (main checkouts only; worktrees share their main repo's config).
find_repos() {
  local root
  for root in "${SCANS[@]}"; do
    [[ -d "$root" ]] || continue
    find "$root" -maxdepth 3 -type d \( -name node_modules -o -name .venv \) -prune \
      -o -type d -name .git -print 2>/dev/null | sed 's|/\.git$||'
  done | sort -u
}

global_hooks_path() { git config --global --get core.hooksPath 2>/dev/null || true; }

scoped_includes() {
  # Prints "key value" for every includeIf that points at our include file.
  git config --global --get-regexp '^includeif\..*\.path$' 2>/dev/null \
    | awk -v f="$INCLUDE_FILE" '$2 == f' || true
}

write_wrapper() {
  local name="$1" file="$HOOKS_DIR/$1"
  cat >"$file" <<EOF
#!/usr/bin/env bash
# $MARK: global $name hook. Do not edit; reinstall from drench44/ci-policy.
CI_POLICY_HOOKS_DIR="\$(cd "\$(dirname "\$0")" && pwd -P)" \\
  exec "$PREFIX/git-hooks/dispatch" $name "\$@"
EOF
  chmod 755 "$file"
}

install_code() {
  local tmp sha
  sha=$(git -C "$SRC" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)
  if [[ $DRY == 1 ]]; then echo "  (dry run) copy $SRC -> $PREFIX ($sha)"; return; fi
  mkdir -p "$(dirname "$PREFIX")"
  tmp=$(mktemp -d "$(dirname "$PREFIX")/.ci-policy-new.XXXXXX")
  mkdir -p "$tmp/lib/ci_policy" "$tmp/policy" "$tmp/git-hooks" "$tmp/bin"
  cp "$SRC"/lib/ci_policy/*.py "$tmp/lib/ci_policy/"
  cp "$SRC/policy/main-allowlist.json" "$tmp/policy/"
  cp "$SRC/git-hooks/dispatch" "$tmp/git-hooks/dispatch"
  cat >"$tmp/bin/ci-policy" <<EOF
#!/usr/bin/env bash
# $MARK: run a ci-policy local check (pre-commit, pre-push, allow-check).
PYTHONPATH="$PREFIX/lib" PYTHONDONTWRITEBYTECODE=1 exec python3 -m ci_policy.local_hooks "\$@"
EOF
  chmod 755 "$tmp/git-hooks/dispatch" "$tmp/bin/ci-policy"
  printf '[core]\n\thooksPath = %s\n' "$HOOKS_DIR" >"$tmp/gitconfig"
  printf 'source=%s\ncommit=%s\ninstalled=%s\n' "$SRC" "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >"$tmp/installed.txt"
  rm -rf "$PREFIX.old"
  [[ -e "$PREFIX" ]] && mv "$PREFIX" "$PREFIX.old"
  mv "$tmp" "$PREFIX"
  rm -rf "$PREFIX.old"
}

install_hooks_dir() {
  local f foreign=()
  if [[ -d "$HOOKS_DIR" ]]; then
    for f in "$HOOKS_DIR"/*; do
      [[ -e "$f" ]] || continue
      grep -q "$MARK" "$f" 2>/dev/null || foreign+=("$f")
    done
  fi
  if ((${#foreign[@]})); then
    [[ $FORCE == 1 ]] || die "$HOOKS_DIR has files ci-policy did not write: ${foreign[*]}. Move them or pass --force (they are backed up to $HOOKS_DIR.bak)."
    run mkdir -p "$HOOKS_DIR.bak"
    for f in "${foreign[@]}"; do run mv "$f" "$HOOKS_DIR.bak/"; done
  fi
  run mkdir -p "$HOOKS_DIR"
  local name
  for name in "${HOOK_NAMES[@]}"; do
    if [[ $DRY == 1 ]]; then echo "  (dry run) write $HOOKS_DIR/$name"; else write_wrapper "$name"; fi
  done
}

point_git_at_hooks() {
  local current
  current=$(global_hooks_path)
  if ((${#SCOPES[@]})); then
    if [[ -n "$current" && "$current" == "$HOOKS_DIR" ]]; then
      say "removing the unscoped global core.hooksPath (switching to scoped)"
      run git config --global --unset core.hooksPath
    elif [[ -n "$current" ]]; then
      say "note: global core.hooksPath is $current (not ours); scoped repos use ours via includeIf"
    fi
    local scope
    for scope in "${SCOPES[@]}"; do
      run git config --global "includeIf.gitdir:$scope/.path" "$INCLUDE_FILE"
      say "scoped: repos under $scope/ use $HOOKS_DIR"
    done
  else
    if [[ -n "$current" && "$current" != "$HOOKS_DIR" && $FORCE != 1 ]]; then
      die "global core.hooksPath is already $current. Pass --force to replace it."
    fi
    run git config --global core.hooksPath "$HOOKS_DIR"
    say "global: core.hooksPath = $HOOKS_DIR"
  fi
}

migrate_repos() {
  local repo local_path
  while IFS= read -r repo; do
    [[ -n "$repo" ]] || continue
    local_path=$(git -C "$repo" config --local --get core.hooksPath 2>/dev/null || true)
    [[ -n "$local_path" ]] || continue
    if [[ "$local_path" == "$HOOKS_DIR" ]]; then
      run git -C "$repo" config --local --unset core.hooksPath
      continue
    fi
    say "migrating $repo: its core.hooksPath ($local_path) now runs as the chained hooks"
    run git -C "$repo" config --local ci-policy.chainHooksPath "$local_path"
    run git -C "$repo" config --local --unset core.hooksPath
  done < <(find_repos)
}

do_install() {
  command -v python3 >/dev/null || die "python3 is required"
  [[ -f "$SRC/lib/ci_policy/local_hooks.py" ]] || die "run this from a ci-policy checkout"
  install_code
  install_hooks_dir
  point_git_at_hooks
  migrate_repos
  say "installed $(sed -n 's/^commit=//p' "$PREFIX/installed.txt" 2>/dev/null || echo '(dry run)') to $PREFIX"
}

do_uninstall() {
  local repo chain key current
  while IFS= read -r repo; do
    [[ -n "$repo" ]] || continue
    chain=$(git -C "$repo" config --local --get ci-policy.chainHooksPath 2>/dev/null || true)
    [[ -n "$chain" ]] || continue
    say "restoring $repo core.hooksPath = $chain"
    run git -C "$repo" config --local core.hooksPath "$chain"
    run git -C "$repo" config --local --unset ci-policy.chainHooksPath
  done < <(find_repos)
  current=$(global_hooks_path)
  [[ "$current" == "$HOOKS_DIR" ]] && run git config --global --unset core.hooksPath
  while read -r key _; do
    [[ -n "$key" ]] || continue
    run git config --global --unset "$key"
  done < <(scoped_includes)
  if [[ -d "$HOOKS_DIR" ]]; then
    local f
    for f in "$HOOKS_DIR"/*; do
      [[ -e "$f" ]] && grep -q "$MARK" "$f" 2>/dev/null && run rm -f "$f"
    done
    run rmdir "$HOOKS_DIR" 2>/dev/null || true
  fi
  [[ -d "$PREFIX" ]] && run rm -rf "$PREFIX"
  say "uninstalled"
}

do_check() {
  local problems=0 current includes repo local_path chain name
  if [[ -f "$PREFIX/installed.txt" ]]; then
    say "code: $PREFIX ($(sed -n 's/^commit=//p' "$PREFIX/installed.txt"))"
  else
    say "PROBLEM: not installed at $PREFIX"; problems=1
  fi
  for name in "${HOOK_NAMES[@]}"; do
    if [[ ! -x "$HOOKS_DIR/$name" ]] || ! grep -q "$MARK" "$HOOKS_DIR/$name"; then
      say "PROBLEM: $HOOKS_DIR/$name missing or not ours"; problems=1
    fi
  done
  current=$(global_hooks_path)
  includes=$(scoped_includes)
  if [[ "$current" == "$HOOKS_DIR" ]]; then
    say "global: core.hooksPath = $HOOKS_DIR"
  elif [[ -n "$includes" ]]; then
    say "scoped:"; printf '%s\n' "$includes" | sed 's/^/  /'
  else
    say "PROBLEM: git is not pointed at $HOOKS_DIR"; problems=1
  fi
  while IFS= read -r repo; do
    [[ -n "$repo" ]] || continue
    local_path=$(git -C "$repo" config --local --get core.hooksPath 2>/dev/null || true)
    chain=$(git -C "$repo" config --local --get ci-policy.chainHooksPath 2>/dev/null || true)
    if [[ -n "$local_path" && "$local_path" != "$HOOKS_DIR" ]]; then
      say "PROBLEM: $repo sets its own core.hooksPath ($local_path), so the policy is skipped there; rerun install"
      problems=1
    elif [[ -n "$chain" ]]; then
      say "ok: $repo chains to $chain"
    fi
  done < <(find_repos)
  return "$problems"
}

case "$MODE" in
  install) do_install ;;
  uninstall) do_uninstall ;;
  check) do_check ;;
esac
