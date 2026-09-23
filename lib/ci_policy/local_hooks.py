"""Checks run by the global git hooks (git-hooks/dispatch) on every machine.

pre-commit   Added lines must not contain an em dash (U+2014) or a secret, and
             no secret-shaped file (.env, id_rsa, ...) may be staged.
pre-push     Refuse any push that updates or deletes main/master, unless every
             pushed commit matches policy/main-allowlist.json for this repo.
             Creating main/master on an empty remote (a brand-new repo) is
             allowed. Break glass (humans only): CI_POLICY_ALLOW_MAIN_PUSH=1.
allow-check  The same allowlist test for a push that has not happened yet, used
             by the Claude Code push guard.

Per-repo settings (git config, so nothing needs committing):
  ci-policy.emdashAllow   glob of files where em dashes are fine (repeatable),
                          for vendored upstream files.
  ci-policy.secretAllow   glob of files the secret scan skips (repeatable),
                          for test fixtures with fake keys.
  ci-policy.repo          owner/name to use for the allowlist when the remote
                          URL is not a GitHub URL.
Files marked linguist-vendored or linguist-generated in .gitattributes skip the
em dash check. A line containing "ci-policy: allow-secret" skips the secret scan.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ci_policy import allowlist
from ci_policy.globs import GlobSet
from ci_policy.pr_policy import gitattributes_globs

EM_DASH = chr(0x2014)  # spelled as a code point so this file has no literal em dash
ZERO = "0" * 40
ALLOW_MARKER = "ci-policy: allow-secret"
DEFAULT_ALLOWLIST = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "policy", "main-allowlist.json")

# High-signal patterns only: a false positive blocks a commit, so every entry
# here has a distinctive prefix or shape. Order: most specific first.
SECRET_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?"
                               r"PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("AWS secret access key", re.compile(
        r"aws_secret_access_key\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}\b", re.I)),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"
                                  r"T3BlbkFJ[A-Za-z0-9_-]{20,}")),
    ("OpenAI project key", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{40,}")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Slack webhook", re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/"
                                 r"[A-Za-z0-9]{20,}")),
    ("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Supabase access or secret key", re.compile(r"\b(?:sbp_[a-f0-9]{40}|sb_secret_[A-Za-z0-9_-]{20,})")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z]{34}\b")),
    ("Tailscale key", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9]{10,}-[A-Za-z0-9]{10,}")),
    ("SendGrid key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b")),
    ("Vercel token", re.compile(r"\bvercel_[A-Za-z0-9_-]{24,}")),
    # Home Assistant long-lived tokens and Supabase service keys are JWTs.
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
                                  r"[A-Za-z0-9_-]{10,}")),
]

# File names that hold secrets by convention, whatever their content.
_SECRET_FILE = re.compile(
    r"(?:^|/)(?:\.env(?:\.(?!example$|sample$|template$|dist$|defaults$)[^/]+)?"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)|[^/]+\.(?:p12|pfx|jks|keystore)"
    r"|\.netrc|\.pgpass|credentials\.json|service[-_]account[^/]*\.json)$")


@dataclass
class Finding:
    kind: str
    path: str
    line: int
    detail: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {where}: {self.kind}{': ' + self.detail if self.detail else ''}"


# ------------------------------------------------------------------ git glue

def git(*args: str, cwd: Optional[str] = None, check: bool = True,
        input_text: Optional[str] = None) -> str:
    proc = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=cwd,
                          input=input_text, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def config_all(key: str, cwd: Optional[str] = None) -> List[str]:
    out = git("config", "--get-all", key, cwd=cwd, check=False)
    items: List[str] = []
    for line in out.splitlines():
        items.extend(p.strip() for p in line.split(",") if p.strip())
    return items


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of unusual paths in diff headers."""
    if not (path.startswith('"') and path.endswith('"')):
        return path
    raw = path[1:-1].encode("latin-1", errors="backslashreplace").decode("unicode_escape")
    return raw.encode("latin-1", errors="replace").decode("utf-8", errors="replace")


@dataclass
class StagedFile:
    path: str
    binary: bool
    added: List[Tuple[int, str]]


def parse_diff(diff: str) -> List[StagedFile]:
    """Parse `git diff -U0 --no-prefix`-style output into added lines per file.

    Tracks state instead of trusting line prefixes, so an added line whose own
    text starts with "++" is never mistaken for a file header.
    """
    files: List[StagedFile] = []
    current: Optional[StagedFile] = None
    in_hunk = False
    new_line = 0
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            current = None
            in_hunk = False
            continue
        if not in_hunk:
            if line.startswith("+++ "):
                # git appends a TAB to header paths that contain a space.
                target = line[4:].rstrip("\t")
                if target != "/dev/null":
                    target = _unquote(target)
                    if target.startswith("b/"):
                        target = target[2:]
                    current = StagedFile(target, False, [])
                    files.append(current)
                continue
            if line.startswith("Binary files ") and line.endswith(" differ"):
                m = re.match(r"Binary files .* and (.*) differ$", line)
                if m and m.group(1) != "/dev/null":
                    target = _unquote(m.group(1).rstrip("\t"))
                    target = target[2:] if target.startswith("b/") else target
                    files.append(StagedFile(target, True, []))
                continue
        if line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            new_line = int(m.group(1)) if m else 0
            in_hunk = current is not None
            continue
        if in_hunk and current is not None:
            if line.startswith("+"):
                current.added.append((new_line, line[1:]))
                new_line += 1
            elif line.startswith(" "):
                new_line += 1
    return files


def staged_names(cwd: Optional[str] = None) -> List[str]:
    """Every staged path except deletions (renames and type changes too)."""
    out = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT", "--find-renames",
              cwd=cwd)
    return [p for p in out.split("\0") if p]


def staged_files(cwd: Optional[str] = None) -> List[StagedFile]:
    diff = git("diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "-U0",
               "--src-prefix=a/", "--dst-prefix=b/", "--diff-filter=ACMRT", "--find-renames",
               cwd=cwd)
    return parse_diff(diff)


BLOB_SCAN_LIMIT = 5 * 1024 * 1024


def staged_blob_text(path: str, cwd: Optional[str] = None) -> Optional[str]:
    """The staged content of a file git called binary, as text, if it really is text.

    A `.gitattributes` entry such as `-diff` or `binary` makes git report a text
    file as binary, which would hide it from the line scan. Real binaries (a NUL
    byte early on) and very large blobs return None.
    """
    size = subprocess.run(["git", "cat-file", "-s", f":{path}"], cwd=cwd, capture_output=True,
                          text=True)
    if size.returncode == 0 and size.stdout.strip().isdigit() and \
            int(size.stdout.strip()) > BLOB_SCAN_LIMIT:
        return None
    proc = subprocess.run(["git", "cat-file", "-p", f":{path}"], cwd=cwd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"could not read the staged {path}: "
                           f"{proc.stderr.decode(errors='replace').strip()}")
    data = proc.stdout
    if len(data) > BLOB_SCAN_LIMIT or b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


def read_gitattributes(cwd: Optional[str] = None) -> str:
    """The committed (HEAD) .gitattributes, or "".

    Not the staged copy: a commit must not be able to exempt itself by adding
    `* linguist-generated` in the same commit. A change takes effect from the
    next commit, the same way pr-policy reads it from the PR's base.
    """
    head = subprocess.run(["git", "show", "HEAD:.gitattributes"], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return head.stdout if head.returncode == 0 else ""


# ------------------------------------------------------------------- checks

def _redact(text: str) -> str:
    return text[:6] + "..." if len(text) > 6 else "..."


def _scan_secrets(path: str, lines: Iterable[Tuple[int, str]]) -> List[Finding]:
    found: List[Finding] = []
    for number, text in lines:
        if ALLOW_MARKER in text:
            continue
        for kind, pattern in SECRET_PATTERNS:
            m = pattern.search(text)
            if m:
                found.append(Finding(kind, path, number, _redact(m.group(0))))
                break
    return found


def scan(files: Sequence[StagedFile], emdash_allow: GlobSet, secret_allow: GlobSet,
         names: Sequence[str] = (), blob_text=None) -> List[Finding]:
    """``blob_text(path)`` returns the full text of a file git called binary, or None."""
    findings: List[Finding] = []
    for path in dict.fromkeys(list(names) + [f.path for f in files]):
        if _SECRET_FILE.search(path) and not secret_allow.matches(path):
            findings.append(Finding("secret file", path, 0,
                                    "files with this name hold secrets; keep it out of git"))
    for f in files:
        check_secret = not secret_allow.matches(f.path)
        if f.binary:
            # Only the secret scan, over the whole file: which lines are new
            # is unknown, and a real binary has no lines at all.
            text = blob_text(f.path) if (blob_text and check_secret) else None
            if text is not None:
                findings.extend(_scan_secrets(f.path, enumerate(text.splitlines(), 1)))
            continue
        if not emdash_allow.matches(f.path):
            for number, text in f.added:
                if EM_DASH in text:
                    col = text.index(EM_DASH)
                    snippet = text[max(0, col - 30):col + 30].strip()
                    findings.append(Finding("em dash", f.path, number, snippet))
        if check_secret:
            findings.extend(_scan_secrets(f.path, f.added))
    return findings


def pre_commit(cwd: Optional[str] = None) -> int:
    files = staged_files(cwd)
    names = staged_names(cwd)
    if not files and not names:
        return 0
    emdash_allow = GlobSet(config_all("ci-policy.emdashAllow", cwd)
                           + gitattributes_globs(read_gitattributes(cwd)))
    secret_allow = GlobSet(config_all("ci-policy.secretAllow", cwd))
    findings = scan(files, emdash_allow, secret_allow, names,
                    blob_text=lambda path: staged_blob_text(path, cwd))
    if not findings:
        return 0
    dashes = [f for f in findings if f.kind == "em dash"]
    secrets = [f for f in findings if f.kind != "em dash"]
    out = sys.stderr
    if secrets:
        print("ci-policy pre-commit: BLOCKED, possible secret in the staged changes:", file=out)
        for f in secrets[:20]:
            print(f.render(), file=out)
        print("  Remove it (and rotate it if it was real). For a fake key in a test, add\n"
              f"  \"{ALLOW_MARKER}\" to that line, or run\n"
              "  git config --add ci-policy.secretAllow '<glob>'.", file=out)
    if dashes:
        print("ci-policy pre-commit: BLOCKED, em dash in added lines (house rule: no em "
              "dashes):", file=out)
        for f in dashes[:20]:
            print(f.render(), file=out)
        if len(dashes) > 20:
            print(f"  ... and {len(dashes) - 20} more", file=out)
        print("  Use a period, comma, colon, or parentheses instead. For a vendored upstream\n"
              "  file: git config --add ci-policy.emdashAllow '<glob>'.", file=out)
    return 1


# --------------------------------------------------------------- push guard

def protected(ref: str) -> Optional[str]:
    """Branch name if ref is a protected branch ref, else None."""
    for name in allowlist.PROTECTED_BRANCHES:
        if ref in (f"refs/heads/{name}", name):
            return name
    return None


def repo_name(remote: str, url: str, cwd: Optional[str] = None) -> Optional[str]:
    """owner/name for the allowlist.

    A GitHub URL always wins, so `git config ci-policy.repo <other repo>` cannot
    borrow another repo's allowlist for a real GitHub remote. The override is
    only for remotes that are not GitHub URLs (tests, mirrors).
    """
    # The configured URL first: git hands hooks the URL after insteadOf
    # rewriting, which may no longer look like GitHub.
    if remote:
        for configured in git("config", "--get-all", f"remote.{remote}.url", cwd=cwd,
                              check=False).splitlines():
            found = allowlist.repo_from_url(configured)
            if found:
                return found
    found = allowlist.repo_from_url(url)
    if found:
        return found
    override = git("config", "--get", "ci-policy.repo", cwd=cwd, check=False).strip()
    return override or None


def _commit_files(sha: str, cwd: Optional[str]) -> List[str]:
    out = git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha, cwd=cwd)
    return [p for p in out.splitlines() if p]


def check_commits(commits: Iterable[str], rules: Sequence[allowlist.Rule], branch: str,
                  cwd: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Return (allowed notes, refused notes) for each commit sha."""
    allowed: List[str] = []
    refused: List[str] = []
    for sha in commits:
        subject, _, who = git("log", "-1", "--format=%s%x00%an%x00%ae", sha,
                              cwd=cwd).rstrip("\n").partition("\0")
        authors = who.split("\0")
        rule = allowlist.match(rules, subject, branch, lambda s=sha: _commit_files(s, cwd),
                               authors)
        line = f"{sha[:9]} {subject}"
        if rule:
            allowed.append(f"{line}  (allowed: {rule.name})")
        else:
            refused.append(line)
    return allowed, refused


def _is_ancestor(old: str, new: str, cwd: Optional[str]) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", old, new], cwd=cwd,
                          capture_output=True).returncode == 0


def _have_object(sha: str, cwd: Optional[str]) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd,
                          capture_output=True).returncode == 0


def remote_heads(remote: str, cwd: Optional[str]) -> List[Tuple[str, str]]:
    """(sha, ref) of every branch on the remote. Raises RuntimeError if unreachable."""
    try:
        proc = subprocess.run(["git", "ls-remote", "--heads", remote], cwd=cwd,
                              capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git ls-remote {remote} timed out") from None
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-remote {remote} failed: {proc.stderr.strip()}")
    return [(line.split()[0], line.split()[1]) for line in proc.stdout.splitlines()
            if len(line.split()) == 2]


def judge_update(local_sha: str, remote_sha: str, branch: str, rules: Sequence[allowlist.Rule],
                 cwd: Optional[str] = None, remote: str = "") -> Tuple[bool, List[str]]:
    """Decide one update of a protected branch. Returns (ok, message lines)."""
    if local_sha == ZERO:
        return False, [f"deleting {branch} on the remote"]
    if remote_sha == ZERO:
        # Creating the branch. Fine on an empty remote (a brand-new repo). On a
        # remote that has branches, every commit not already on a PROTECTED
        # branch there must pass the allowlist: being on some feature branch
        # does not make a commit reviewed.
        heads = remote_heads(remote, cwd) if remote else []
        if not heads:
            return True, [f"creating {branch} on a remote with no branches yet (new repo)"]
        guarded = [sha for sha, ref in heads if protected(ref)]
        missing = [sha for sha in guarded if not _have_object(sha, cwd)]
        if missing:
            return False, [f"creating {branch}: the remote has protected branches this clone "
                           f"has not fetched ({missing[0][:9]}); fetch first"]
        commits = git("rev-list", "--reverse", local_sha,
                      *(["--not", *guarded] if guarded else []), cwd=cwd).split()
        if not commits:
            return True, [f"creating {branch} at a commit already on a protected branch"]
        allowed, refused = check_commits(commits, rules, branch, cwd)
        if refused:
            return False, ([f"creating {branch} would land {len(refused)} commit(s) without "
                            "a PR:"] + [f"    {r}" for r in refused[:15]])
        return True, ["every commit is on the main-branch allowlist:"] + [
            f"    {a}" for a in allowed]
    if not _have_object(remote_sha, cwd):
        return False, [f"the remote {branch} is at {remote_sha[:9]}, which this clone does "
                       "not have; fetch first. A push that replaces unknown history is a "
                       "force push"]
    if not _is_ancestor(remote_sha, local_sha, cwd):
        return False, [f"force push: the remote {branch} ({remote_sha[:9]}) is not part of "
                       "what you are pushing, so history would be rewritten"]
    commits = git("rev-list", "--reverse", f"{remote_sha}..{local_sha}", cwd=cwd).split()
    if not commits:
        return True, [f"nothing new for {branch}"]
    allowed, refused = check_commits(commits, rules, branch, cwd)
    if refused:
        return False, ([f"{len(refused)} commit(s) would land on {branch} without a PR:"]
                       + [f"    {r}" for r in refused[:15]]
                       + ([f"    ... and {len(refused) - 15} more"] if len(refused) > 15 else []))
    return True, ["every commit is on the main-branch allowlist:"] + [f"    {a}" for a in allowed]


def load_rules(repo: Optional[str], path: Optional[str] = None) -> List[allowlist.Rule]:
    """Allow rules for this repo. A missing or broken allowlist raises (fail closed)."""
    override = os.environ.get("CI_POLICY_ALLOWLIST")
    if override and not path:
        print(f"ci-policy: using the allowlist from CI_POLICY_ALLOWLIST={override}",
              file=sys.stderr)
    path = path or override or DEFAULT_ALLOWLIST
    if not os.path.exists(path):
        raise OSError(f"the allowlist {path} is missing; reinstall ci-policy")
    return allowlist.rules_for(allowlist.load(path), repo)


def pre_push(remote: str, url: str, lines: Iterable[str], cwd: Optional[str] = None) -> int:
    updates = []
    for raw in lines:
        parts = raw.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        branch = protected(remote_ref)
        if branch:
            updates.append((local_ref, local_sha, remote_ref, remote_sha, branch))
    if not updates:
        return 0
    repo = repo_name(remote, url, cwd)
    try:
        rules = load_rules(repo)
    except (OSError, ValueError) as e:
        print(f"ci-policy pre-push: cannot read the allowlist ({e}); refusing the push to a "
              "protected branch.", file=sys.stderr)
        return 1
    status = 0
    for local_ref, local_sha, remote_ref, remote_sha, branch in updates:
        ok, lines_out = judge_update(local_sha, remote_sha, branch, rules, cwd,
                                     remote or url)
        if ok:
            print(f"ci-policy pre-push: allowed push to {branch}: " + "\n".join(lines_out),
                  file=sys.stderr)
            continue
        if os.environ.get("CI_POLICY_ALLOW_MAIN_PUSH") == "1":
            print(f"ci-policy pre-push: WARNING: CI_POLICY_ALLOW_MAIN_PUSH=1, so pushing to "
                  f"{branch} anyway: " + "\n".join(lines_out), file=sys.stderr)
            continue
        status = 1
        print(f"ci-policy pre-push: BLOCKED push to {branch} of {repo or remote}: "
              + "\n".join(lines_out), file=sys.stderr)
    if status:
        print("  Push a branch and open a PR instead: git push -u origin HEAD:<branch-name>\n"
              "  Automation that must commit to main belongs in policy/main-allowlist.json\n"
              "  (drench44/ci-policy). Humans can break glass with\n"
              "  CI_POLICY_ALLOW_MAIN_PUSH=1 git push ...", file=sys.stderr)
    return status


def allow_check(remote: str, branch: str, src: str, cwd: Optional[str] = None) -> int:
    """For the Claude guard: 0 if pushing ``src`` to ``remote``/``branch`` is allowed.

    Uses the remote-tracking ref as the remote's current tip (the guard runs
    before the push, so there is no pre-push stdin yet). Exit 0 allowed,
    1 refused, 2 could not decide.
    """
    try:
        local_sha = git("rev-parse", "--verify", f"{src}^{{commit}}", cwd=cwd).strip()
    except RuntimeError as e:
        print(f"cannot resolve {src}: {e}", file=sys.stderr)
        return 2
    tracking = f"refs/remotes/{remote}/{branch}"
    remote_sha = git("rev-parse", "--verify", "--quiet", tracking, cwd=cwd, check=False).strip()
    if not remote_sha:
        listed = subprocess.run(["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"],
                                cwd=cwd, capture_output=True, text=True, timeout=20)
        if listed.returncode != 0:
            print(f"cannot reach {remote}: {listed.stderr.strip()}", file=sys.stderr)
            return 2
        if listed.stdout.strip():
            print(f"{remote}/{branch} exists but is not fetched; run git fetch {remote}",
                  file=sys.stderr)
            return 2
        remote_sha = ZERO
    try:
        url = remote if ("/" in remote or ":" in remote) else ""
        rules = load_rules(repo_name(remote, url, cwd))
    except (OSError, ValueError) as e:
        print(f"cannot read the allowlist: {e}", file=sys.stderr)
        return 2
    ok, lines_out = judge_update(local_sha, remote_sha, branch, rules, cwd, remote)
    print("\n".join(lines_out), file=sys.stderr)
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ci-policy")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pre-commit")
    p = sub.add_parser("pre-push")
    p.add_argument("remote", nargs="?", default="")
    p.add_argument("url", nargs="?", default="")
    a = sub.add_parser("allow-check")
    a.add_argument("--remote", required=True)
    a.add_argument("--branch", required=True)
    a.add_argument("--src", default="HEAD")
    a.add_argument("-C", dest="cwd", default=None)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "pre-commit":
            return pre_commit()
        if args.cmd == "pre-push":
            return pre_push(args.remote, args.url, sys.stdin.read().splitlines())
        return allow_check(args.remote, args.branch, args.src, args.cwd)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"ci-policy {args.cmd}: could not run the check ({e}); refusing (fail closed). "
              "Bypass for one commit with --no-verify if you are sure.", file=sys.stderr)
        return 2 if args.cmd == "allow-check" else 1


if __name__ == "__main__":
    sys.exit(main())
