#!/usr/bin/env bash
# End-to-end tests for the global git hooks: the installer, the dispatcher,
# the pre-commit policy, the pre-push guard, and chaining to repo hooks.
# Real git against throwaway repos and bare remotes; HOME is a temp dir, so
# the machine's own ~/.gitconfig is never touched. Plain bash, no bats needed.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
PASS=0 FAIL=0 CURRENT=""
EMDASH=$(printf '\342\200\224')

fail() { printf '  FAIL [%s]: %s\n' "$CURRENT" "$*"; FAIL=$((FAIL + 1)); }
ok()   { PASS=$((PASS + 1)); }
assert_eq()       { [[ "$1" == "$2" ]] && ok || fail "expected '$2', got '$1' ${3:-}"; }
assert_contains() { [[ "$1" == *"$2"* ]] && ok || fail "expected to find '$2' in: ${1:0:800}"; }
assert_not_contains() { [[ "$1" != *"$2"* ]] && ok || fail "did not expect '$2' in: ${1:0:800}"; }

# A fresh machine: temp HOME, ci-policy installed globally, one repo "r" with a
# bare remote "origin" that already has main.
setup() {
  CURRENT="$1"
  T=$(mktemp -d)
  export HOME="$T/home" GIT_CONFIG_NOSYSTEM=1
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE CI_POLICY_ALLOW_MAIN_PUSH CI_POLICY_ALLOWLIST \
    CI_POLICY_DISPATCH_DEPTH CI_POLICY_HOME CI_POLICY_HOOKS_DIR
  mkdir -p "$HOME/Documents"
  git config --global user.email t@example.com
  git config --global user.name t
  git config --global init.defaultBranch main
  git config --global protocol.file.allow always
  R="$HOME/Documents/r"
  git init -q --bare "$T/origin.git"
  git init -q "$R"
  git -C "$R" remote add origin "$T/origin.git"
  echo one >"$R/a.txt"
  git -C "$R" add a.txt
  git -C "$R" commit -q -m one
  git -C "$R" push -q origin main 2>/dev/null
  git -C "$R" config ci-policy.repo drench44/test-repo
}

install() { OUT=$(bash "$SRC/install/install-git-hooks.sh" "$@" 2>&1); RC=$?; }

# commit FILE CONTENT [message]: sets RC and OUT.
commit() {
  printf '%s\n' "$2" >"$R/$1"
  git -C "$R" add -- "$1"
  OUT=$(git -C "$R" commit -m "${3:-change $1}" 2>&1); RC=$?
}

push() { OUT=$(git -C "$R" push "$@" 2>&1); RC=$?; }

teardown() { rm -rf "$T"; }

# ---------------------------------------------------------------- install

setup "install sets core.hooksPath, copies code, writes wrappers"
install
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global core.hooksPath)" "$HOME/.config/git/hooks"
[[ -x "$HOME/.config/git/hooks/pre-commit" && -x "$HOME/.config/git/hooks/pre-push" ]] && ok || fail "wrappers missing"
[[ -f "$HOME/.config/ci-policy/lib/ci_policy/local_hooks.py" ]] && ok || fail "lib missing"
[[ -f "$HOME/.config/ci-policy/policy/main-allowlist.json" ]] && ok || fail "allowlist missing"
OUT=$(bash "$SRC/install/install-git-hooks.sh" --check 2>&1); assert_eq "$?" 0 "$OUT"
teardown

setup "install refuses to replace a foreign core.hooksPath without --force"
git config --global core.hooksPath /somewhere/else
install
assert_eq "$RC" 1
assert_contains "$OUT" "already /somewhere/else"
install --force
assert_eq "$RC" 0 "$OUT"
teardown

setup "install refuses to clobber foreign files in the hooks dir"
mkdir -p "$HOME/.config/git/hooks"; echo mine >"$HOME/.config/git/hooks/pre-commit"
install
assert_eq "$RC" 1
assert_contains "$OUT" "did not write"
install --force
assert_eq "$RC" 0 "$OUT"
assert_eq "$(cat "$HOME/.config/git/hooks.bak/pre-commit")" "mine"
teardown

setup "reinstall is idempotent"
install; install
assert_eq "$RC" 0 "$OUT"
teardown

setup "dry run changes nothing"
install --dry-run
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global core.hooksPath)" ""
[[ ! -e "$HOME/.config/ci-policy" ]] && ok || fail "dry run wrote files"
teardown

# ---------------------------------------------------------------- pre-commit

setup "em dash in an added line is blocked"
install
commit b.txt "a ${EMDASH} b"
assert_eq "$RC" 1
assert_contains "$OUT" "em dash"
assert_contains "$OUT" "b.txt:1"
teardown

setup "clean commit passes; removing an em dash line passes"
printf 'x %s y\n' "$EMDASH" >"$R/old.txt"; git -C "$R" add old.txt; git -C "$R" commit -q -m old --no-verify
install
commit b.txt "plain text"
assert_eq "$RC" 0 "$OUT"
git -C "$R" rm -q old.txt
OUT=$(git -C "$R" commit -m "drop old" 2>&1); assert_eq "$?" 0 "$OUT"
teardown

setup "em dash allowed by ci-policy.emdashAllow and by linguist-vendored"
install
git -C "$R" config --add ci-policy.emdashAllow 'vendor/**'
mkdir -p "$R/vendor"
commit vendor/up.html "a ${EMDASH} b"
assert_eq "$RC" 0 "$OUT"
printf 'board.html linguist-vendored\n' >"$R/.gitattributes"; git -C "$R" add .gitattributes
commit board.html "a ${EMDASH} b"
assert_eq "$RC" 0 "$OUT"
teardown

setup "secret in an added line is blocked, redacted, and the marker allows a fake"
install
tok="ghp_$(printf 'A%.0s' {1..36})"
commit c.py "TOKEN = '$tok'"
assert_eq "$RC" 1
assert_contains "$OUT" "GitHub token"
assert_not_contains "$OUT" "$tok"
git -C "$R" reset -q
commit c.py "TOKEN = '$tok'  # ci-policy: allow-secret"
assert_eq "$RC" 0 "$OUT"
teardown

setup "secret-named files are blocked, examples are fine"
install
commit .env "HA_TOKEN=x"
assert_eq "$RC" 1
assert_contains "$OUT" "secret file"
git -C "$R" reset -q; rm "$R/.env"
commit .env.example "HA_TOKEN=put-yours-here"
assert_eq "$RC" 0 "$OUT"
teardown

setup "renaming a file to .env with no content change is still caught"
install
commit settings.txt "HA_TOKEN=x"
git -C "$R" mv settings.txt .env
OUT=$(git -C "$R" commit -m "rename" 2>&1); assert_eq "$?" 1
assert_contains "$OUT" "secret file"
teardown

setup "--no-verify still bypasses pre-commit (human escape hatch)"
install
printf 'a %s b\n' "$EMDASH" >"$R/b.txt"; git -C "$R" add b.txt
OUT=$(git -C "$R" commit -q -m x --no-verify 2>&1); assert_eq "$?" 0 "$OUT"
teardown

# ---------------------------------------------------------------- chaining

setup "repo .git/hooks/pre-commit still runs, after the policy"
cat >"$R/.git/hooks/pre-commit" <<'EOF'
#!/bin/sh
echo local-pre-commit-ran >"$(git rev-parse --git-dir)/marker"
exit 0
EOF
chmod +x "$R/.git/hooks/pre-commit"
install
commit b.txt "fine"
assert_eq "$RC" 0 "$OUT"
[[ -f "$R/.git/marker" ]] && ok || fail "local hook did not run"
teardown

setup "a failing repo hook still blocks the commit"
printf '#!/bin/sh\necho repo-says-no >&2\nexit 1\n' >"$R/.git/hooks/pre-commit"
chmod +x "$R/.git/hooks/pre-commit"
install
commit b.txt "fine"
assert_eq "$RC" 1
assert_contains "$OUT" "repo-says-no"
teardown

setup "the repo hook does not run when the policy already blocked"
printf '#!/bin/sh\ntouch "$(git rev-parse --git-dir)/marker"\n' >"$R/.git/hooks/pre-commit"
chmod +x "$R/.git/hooks/pre-commit"
install
commit b.txt "a ${EMDASH} b"
assert_eq "$RC" 1
[[ ! -f "$R/.git/marker" ]] && ok || fail "local hook ran after a policy block"
teardown

setup "hooks without policy (commit-msg, post-commit) chain with args"
printf '#!/bin/sh\ncp "$1" "$(git rev-parse --git-dir)/msg-copy"\n' >"$R/.git/hooks/commit-msg"
printf '#!/bin/sh\ntouch "$(git rev-parse --git-dir)/post"\n' >"$R/.git/hooks/post-commit"
chmod +x "$R/.git/hooks/commit-msg" "$R/.git/hooks/post-commit"
install
commit b.txt "fine" "hello message"
assert_eq "$RC" 0 "$OUT"
assert_contains "$(cat "$R/.git/msg-copy" 2>/dev/null)" "hello message"
[[ -f "$R/.git/post" ]] && ok || fail "post-commit did not chain"
teardown

setup "a non-executable or sample repo hook is ignored"
printf '#!/bin/sh\nexit 1\n' >"$R/.git/hooks/pre-commit"; chmod -x "$R/.git/hooks/pre-commit"
install
commit b.txt "fine"
assert_eq "$RC" 0 "$OUT"
teardown

setup "repo with its own core.hooksPath is migrated and its hooks still run"
mkdir -p "$R/.githooks"
printf '#!/bin/sh\ntouch "$(git rev-parse --git-dir)/githooks-ran"\n' >"$R/.githooks/pre-commit"
chmod +x "$R/.githooks/pre-commit"
git -C "$R" config core.hooksPath .githooks
install
assert_contains "$OUT" "migrating $R"
assert_eq "$(git -C "$R" config --local --get core.hooksPath)" ""
assert_eq "$(git -C "$R" config --get ci-policy.chainHooksPath)" ".githooks"
commit b.txt "fine"
assert_eq "$RC" 0 "$OUT"
[[ -f "$R/.git/githooks-ran" ]] && ok || fail ".githooks/pre-commit did not run"
commit c.txt "a ${EMDASH} b"
assert_eq "$RC" 1 "(policy runs in a migrated repo)"
teardown

setup "check flags a repo whose own core.hooksPath shadows the policy"
install
git -C "$R" config core.hooksPath .githooks
OUT=$(bash "$SRC/install/install-git-hooks.sh" --check 2>&1); assert_eq "$?" 1
assert_contains "$OUT" "sets its own core.hooksPath"
teardown

setup "chaining works from a linked worktree"
printf '#!/bin/sh\ntouch "%s/wt-marker"\n' "$T" >"$R/.git/hooks/pre-commit"
chmod +x "$R/.git/hooks/pre-commit"
install
git -C "$R" worktree add -q "$HOME/Documents/r-wt" -b wt 2>/dev/null
echo x >"$HOME/Documents/r-wt/w.txt"; git -C "$HOME/Documents/r-wt" add w.txt
OUT=$(git -C "$HOME/Documents/r-wt" commit -m wt 2>&1); assert_eq "$?" 0 "$OUT"
[[ -f "$T/wt-marker" ]] && ok || fail "worktree commit did not chain"
teardown

setup "chainHooksPath pointing back at the global hooks does not loop"
install
git -C "$R" config ci-policy.chainHooksPath "$HOME/.config/git/hooks"
commit b.txt "fine"
assert_eq "$RC" 0 "$OUT"
teardown

# ---------------------------------------------------------------- pre-push

setup "push to main is refused, a feature branch is fine"
install
commit b.txt "fine"
push origin main
assert_eq "$RC" 1
assert_contains "$OUT" "BLOCKED push to main"
push origin HEAD:refs/heads/feature
assert_eq "$RC" 0 "$OUT"
teardown

setup "HEAD:main, refs/heads/main, master and --force are refused"
git -C "$R" push -q origin main:master 2>/dev/null
install
commit b.txt "fine"
push origin HEAD:main;               assert_eq "$RC" 1 "(HEAD:main)"
push origin HEAD:refs/heads/main;    assert_eq "$RC" 1 "(refs/heads/main)"
push origin HEAD:master;             assert_eq "$RC" 1 "(master)"
push --force origin HEAD:main;       assert_eq "$RC" 1 "(--force)"
git -C "$R" reset -q --hard HEAD~1
echo other >"$R/o.txt"; git -C "$R" add o.txt; git -C "$R" commit -q -m other --no-verify
git -C "$R" checkout -q -b tmp; git -C "$R" checkout -q main
push --force origin main
assert_eq "$RC" 1
assert_contains "$OUT" "BLOCKED"
teardown

setup "deleting main on the remote is refused"
install
push origin :main
assert_eq "$RC" 1
assert_contains "$OUT" "deleting main"
teardown

setup "creating main on an empty remote is allowed (new repo)"
install
git init -q --bare "$T/empty.git"
git -C "$R" remote add empty "$T/empty.git"
push empty main
assert_eq "$RC" 0 "$OUT"
teardown

setup "allowlisted release commit may go to main; the same subject touching more may not"
install
git -C "$R" config ci-policy.repo drench44/family-hub
mkdir -p "$R/src/family_hub/web/static"
echo 1.4.1 >"$R/VERSION"; echo log >"$R/CHANGELOG.md"; echo html >"$R/src/family_hub/web/static/index.html"
git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin main
assert_eq "$RC" 0 "$OUT"
assert_contains "$OUT" "allowed: release script"
echo 1.4.2 >"$R/VERSION"; echo code >"$R/app.py"
git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.2"
push origin main
assert_eq "$RC" 1
assert_contains "$OUT" "release: v1.4.2"
teardown

setup "allowlist is per repo: the same release commit in another repo is refused"
install
echo 1.4.1 >"$R/VERSION"
git -C "$R" add -A; git -C "$R" commit -q --no-verify -m "release: v1.4.1"
push origin main
assert_eq "$RC" 1 "$OUT"
teardown

setup "repo name comes from a GitHub remote URL"
install
git -C "$R" config --unset ci-policy.repo
git -C "$R" config url."$T/origin.git".insteadOf "https://github.com/drench44/claude-config-backup.git"
git -C "$R" remote set-url origin "https://github.com/drench44/claude-config-backup.git"
git -C "$R" push -q origin main:master 2>/dev/null
echo mem >"$R/m.md"; git -C "$R" add m.md; git -C "$R" commit -q --no-verify -m "memory: note"
push origin HEAD:master
assert_eq "$RC" 0 "$OUT"
echo x >"$R/n.md"; git -C "$R" add n.md; git -C "$R" commit -q --no-verify -m "tweak settings"
push origin HEAD:master
assert_eq "$RC" 1 "$OUT"
teardown

setup "break glass: CI_POLICY_ALLOW_MAIN_PUSH=1"
install
commit b.txt "fine"
OUT=$(CI_POLICY_ALLOW_MAIN_PUSH=1 git -C "$R" push origin main 2>&1); assert_eq "$?" 0 "$OUT"
assert_contains "$OUT" "WARNING"
teardown

setup "the repo pre-push hook still runs with the ref lines on stdin"
cat >"$R/.git/hooks/pre-push" <<EOF
#!/bin/sh
cat >"$T/prepush-stdin"
echo "\$1 \$2" >"$T/prepush-args"
EOF
chmod +x "$R/.git/hooks/pre-push"
install
commit b.txt "fine"
push origin HEAD:refs/heads/feature
assert_eq "$RC" 0 "$OUT"
assert_contains "$(cat "$T/prepush-stdin" 2>/dev/null)" "refs/heads/feature"
assert_eq "$(cat "$T/prepush-args" 2>/dev/null)" "origin $T/origin.git"
teardown

setup "a broken install fails closed"
install
rm -rf "$HOME/.config/ci-policy/lib"
commit b.txt "fine"
assert_eq "$RC" 1
assert_contains "$OUT" "fail closed"
teardown

# ---------------------------------------------------------------- scope + uninstall

setup "scoped install only covers repos under the scope"
mkdir -p "$HOME/workspace"
install --scope "$HOME/workspace"
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global core.hooksPath)" ""
commit b.txt "a ${EMDASH} b"
assert_eq "$RC" 0 "(outside the scope nothing runs)"
W="$HOME/workspace/w"; git init -q "$W"
printf 'a %s b\n' "$EMDASH" >"$W/x.txt"; git -C "$W" add x.txt
OUT=$(git -C "$W" commit -m x 2>&1); assert_eq "$?" 1 "(inside the scope the policy runs)"
OUT=$(bash "$SRC/install/install-git-hooks.sh" --scope "$HOME/workspace" --check 2>&1)
assert_eq "$?" 0 "$OUT"
teardown

setup "uninstall restores repo hooks and removes everything"
mkdir -p "$R/.githooks"; git -C "$R" config core.hooksPath .githooks
install
install --uninstall
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global core.hooksPath)" ""
assert_eq "$(git -C "$R" config --local core.hooksPath)" ".githooks"
assert_eq "$(git -C "$R" config --local ci-policy.chainHooksPath)" ""
[[ ! -e "$HOME/.config/ci-policy" && ! -e "$HOME/.config/git/hooks" ]] && ok || fail "files left behind"
teardown

setup "uninstall of a scoped install removes the includeIf"
mkdir -p "$HOME/workspace"
install --scope "$HOME/workspace"
install --scope "$HOME/workspace" --uninstall
assert_eq "$RC" 0 "$OUT"
assert_eq "$(git config --global --get-regexp '^includeif' || true)" ""
teardown

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
(( FAIL == 0 ))
