"""main-watch: alert when commits reach the default branch without a green PR.

Runs on ``push`` to the default branch. For every commit the push added:

* pass if it matches an allow rule (subject regex, optional author, optional
  "only these paths changed" globs), for example release bumps; or
* pass if it reached the branch through a PR merged into it (directly, or as
  part of a stacked PR that was merged into another PR's branch first) whose
  head commit's checks were ALL green: every check run and commit status
  concluded success, neutral or skipped, every ``required-checks`` name is
  there and green, and at least one check passed;
* wait if nothing failed but some checks were still running when the PR was
  merged (automation that merges without waiting). The commit gets a pending
  ``ci-policy/main-watch`` status and the scheduled re-check reads the checks
  again, until ``pending-timeout-minutes`` after the merge;
* otherwise it is flagged.

A force push (the old tip is not an ancestor of the new one) and a deleted
default branch are flagged too. Flags open one issue labeled ``main-watch``,
or add a comment to the one already open. It alerts only; it never reverts.

Every commit it judges gets a ``ci-policy/main-watch`` commit status (success,
failure or pending). ``main_watch_audit`` reads those statuses from outside the
homelab to notice when main-watch did not run at all.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from ci_policy import allowlist, gh
from ci_policy.globs import GlobSet, split_list

ZERO_SHA = "0" * 40
OK_CONCLUSIONS = {"success", "neutral", "skipped"}
# A check suite that finished with one of these and never created a check run
# is a workflow that could not start (bad YAML, approval needed) or timed out.
# Cancelled and stale suites with no runs are left out: a run cancelled before
# its first job is how concurrency supersedes a run on the same commit.
BAD_EMPTY_SUITE = {"failure", "startup_failure", "timed_out", "action_required"}
STATUS_CONTEXT = "ci-policy/main-watch"
PASS, FLAG, WAIT = "pass", "flag", "pending"
GREEN, RED, PENDING = "green", "red", "pending"
STATUS_STATE = {PASS: "success", FLAG: "failure", WAIT: "pending"}
PR_REF = re.compile(r"^PR #(\d+)\b")
# The shared policy jobs (`<caller job> / pr-policy`, `... / main-watch`) run on
# every PR whatever the change, so they must be green but do not count as the
# "at least one check passed" the PR's own CI has to provide.
POLICY_JOB = re.compile(r"(^|/ )(pr-policy|main-watch)$")
# How many of the branch's newest commits the scheduled re-check reads.
RECHECK_COMMITS = 50
MAX_STACK_DEPTH = 4


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Config:
    allow: List[allowlist.Rule] = field(default_factory=list)
    ignore_checks: GlobSet = field(default_factory=lambda: GlobSet([]))
    # Exact check run names or commit status contexts that must be present and
    # green on the PR head.
    required_checks: List[str] = field(default_factory=list)
    # True, False, or None for "auto": required when the repo has any workflow
    # besides the one running main-watch.
    require_checks: Optional[bool] = True
    pending_timeout_minutes: int = 180
    fail_on_alert: bool = True
    label: str = "main-watch"
    run_id: str = ""
    # This run's own workflow file (from GITHUB_WORKFLOW_REF), left out when
    # "auto" asks whether the repo has other workflows.
    workflow_path: str = ""
    set_status: bool = True
    target_url: str = ""
    # Looked up at call time so tests can patch main_watch.utcnow.
    now: Callable[[], dt.datetime] = field(default=lambda: utcnow())


@dataclass
class Commit:
    sha: str
    subject: str
    author_login: Optional[str]
    author_name: str
    author_email: str

    @property
    def short(self) -> str:
        return self.sha[:7]


@dataclass
class Verdict:
    commit: Commit
    state: str          # PASS, FLAG or WAIT
    reason: str
    pr: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.state == PASS


@dataclass
class ChecksResult:
    state: str          # GREEN, RED or PENDING
    detail: str


@dataclass
class PushPlan:
    commits: List[Commit]
    alerts: List[str]   # push-level problems (force push, deletion)
    notes: List[str]    # informational


def parse_allow_rules(rules_json: str, allowlist_file: str = "", repo: str = "") -> List[allowlist.Rule]:
    """Rules from the shared allowlist file (entries for ``repo``) plus ``allow-rules`` JSON."""
    rules: List[allowlist.Rule] = []
    if allowlist_file.strip():
        rules.extend(allowlist.rules_for(allowlist.load(allowlist_file.strip()), repo))
    if rules_json.strip():
        try:
            data = json.loads(rules_json)
        except ValueError as e:
            raise ValueError(f"allow-rules is not valid JSON: {e}") from None
        if not isinstance(data, list):
            raise ValueError("allow-rules must be a JSON list of objects")
        rules.extend(allowlist.make_rule(item, f"allow-rules[{i}]")
                     for i, item in enumerate(data, 1))
    return rules


def parse_required_checks(raw: str) -> List[str]:
    """One check name per line. Commas stay: check names may contain them."""
    names = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in names:
            names.append(line)
    return names


def parse_require_checks(raw: str) -> Optional[bool]:
    value = (raw or "").strip().lower()
    if value in ("", "auto"):
        return None
    if value in ("true", "yes", "1", "on"):
        return True
    if value in ("false", "no", "0", "off"):
        return False
    raise ValueError(f"input require-checks: expected auto, true or false, got {raw!r}")


def workflow_path_from_ref(ref: str) -> str:
    """``owner/repo/.github/workflows/x.yml@refs/heads/main`` -> ``.github/workflows/x.yml``."""
    path = ref.split("@", 1)[0]
    parts = path.split("/", 2)
    return parts[2] if len(parts) == 3 else ""


def commit_from_api(c: Dict[str, Any]) -> Commit:
    info = c.get("commit") or {}
    author = info.get("author") or {}
    message = info.get("message") or ""
    return Commit(sha=c["sha"], subject=message.splitlines()[0] if message else "",
                  author_login=(c.get("author") or {}).get("login"),
                  author_name=author.get("name") or "", author_email=author.get("email") or "")


def commit_from_event(c: Dict[str, Any]) -> Commit:
    author = c.get("author") or {}
    message = c.get("message") or ""
    return Commit(sha=c["id"], subject=message.splitlines()[0] if message else "",
                  author_login=author.get("username"),
                  author_name=author.get("name") or "", author_email=author.get("email") or "")


def plan_push(event: Dict[str, Any], api: gh.GitHub, repo: str) -> PushPlan:
    """Work out which commits this push added to the branch."""
    before, after = event.get("before") or ZERO_SHA, event.get("after") or ZERO_SHA
    alerts: List[str] = []
    notes: List[str] = []
    event_commits = [commit_from_event(c) for c in event.get("commits") or []]

    if after == ZERO_SHA or event.get("deleted"):
        alerts.append(f"The branch was deleted (old tip {before[:7]}).")
        return PushPlan([], alerts, notes)

    if before == ZERO_SHA or event.get("created"):
        commits = event_commits or ([commit_from_event(event["head_commit"])]
                                    if event.get("head_commit") else [])
        notes.append("First push of this branch, so there is no old tip to compare with. "
                      "Checked the commits listed in the push event"
                      + (" (GitHub lists at most 20)." if len(commits) >= 20 else "."))
        return PushPlan(commits, alerts, notes)

    try:
        compare = api.get(f"/repos/{repo}/compare/{before}...{after}", {"per_page": 100})
    except gh.GitHubError as e:
        if e.status != 404:
            raise
        # The old tip is gone (typical after a force push and cleanup).
        alerts.append(f"Force push suspected: the old tip {before[:7]} no longer exists.")
        notes.append("Could not compare with the old tip, so only the commits listed in the "
                     "push event were checked (GitHub lists at most 20).")
        return PushPlan(event_commits, alerts, notes)

    status = compare.get("status")
    if status in ("diverged", "behind") or event.get("forced"):
        alerts.append(f"Force push: the old tip {before[:7]} is not part of the new history "
                      f"(compare status: {status}). History on this branch was rewritten.")
    total = int(compare.get("total_commits") or 0)
    raw = list(compare.get("commits") or [])
    page = 2
    while len(raw) < total:
        more = api.get(f"/repos/{repo}/compare/{before}...{after}",
                       {"per_page": 100, "page": page}).get("commits") or []
        if not more:
            break
        raw.extend(more)
        page += 1
    if len(raw) < total:
        notes.append(f"GitHub returned {len(raw)} of {total} commits; the rest were not checked.")
        alerts.append(f"Only {len(raw)} of {total} pushed commits could be checked.")
    return PushPlan([commit_from_api(c) for c in raw], alerts, notes)


def allow_match(commit: Commit, rules: Sequence[allowlist.Rule], files_for,
                branch: str) -> Optional[allowlist.Rule]:
    authors = [commit.author_login or "", commit.author_name, commit.author_email]
    return allowlist.match(rules, commit.subject, branch, lambda: files_for(commit.sha), authors)


def _shown(items: List[str], limit: int = 6) -> str:
    return "; ".join(items[:limit]) + (f"; and {len(items) - limit} more"
                                       if len(items) > limit else "")


def _minutes(delta: dt.timedelta) -> int:
    return int(delta.total_seconds() // 60)


class Watcher:
    """Evaluates commits against the API, caching per-PR check results."""

    def __init__(self, api: gh.GitHub, repo: str, branch: str, cfg: Config):
        self.api, self.repo, self.branch, self.cfg = api, repo, branch, cfg
        self.owner = repo.split("/", 1)[0]
        self._pr_checks: Dict[int, ChecksResult] = {}
        self._files: Dict[str, List[str]] = {}
        self._pulls: Dict[str, List[Dict[str, Any]]] = {}
        self._contains: Dict[Tuple[str, str], bool] = {}
        self._require: Optional[bool] = None
        # PRs merged into the branch that this run already found; a commit of a
        # stacked PR reached the branch inside one of them.
        self.carriers: List[Dict[str, Any]] = []

    # ------------------------------------------------------------ lookups

    def files_for(self, sha: str) -> List[str]:
        if sha not in self._files:
            data = self.api.get(f"/repos/{self.repo}/commits/{sha}")
            files = data.get("files") or []
            if len(files) >= 300:
                # GitHub truncates the list here; an unseen file could break the
                # rule, so a path-limited allow rule must not match.
                self._files[sha] = []
                return self._files[sha]
            self._files[sha] = [f["filename"] for f in files] + \
                [f["previous_filename"] for f in files if f.get("previous_filename")]
        return self._files[sha]

    def pulls_for(self, sha: str) -> List[Dict[str, Any]]:
        if sha not in self._pulls:
            self._pulls[sha] = self.api.get(f"/repos/{self.repo}/commits/{sha}/pulls") or []
        return self._pulls[sha]

    def merged_into(self, pulls: List[Dict[str, Any]], branch: str) -> List[Dict[str, Any]]:
        return [p for p in pulls
                if p.get("merged_at") and (p.get("base") or {}).get("ref") == branch]

    def contains(self, head: str, sha: str) -> bool:
        """Is ``sha`` the commit ``head`` or one of its ancestors?"""
        if head == sha:
            return True
        key = (head, sha)
        if key not in self._contains:
            try:
                data = self.api.get(f"/repos/{self.repo}/compare/{sha}...{head}")
                self._contains[key] = data.get("status") in ("ahead", "identical")
            except gh.GitHubError as e:
                if e.status != 404:
                    raise
                self._contains[key] = False
        return self._contains[key]

    def checks_required(self) -> bool:
        if self.cfg.require_checks is not None:
            return self.cfg.require_checks
        if self._require is None:
            try:
                flows = self.api.paginate(f"/repos/{self.repo}/actions/workflows",
                                          item_key="workflows")
            except gh.GitHubError as e:
                gh.annotate("warning", f"require-checks is auto but the workflow list could "
                            f"not be read ({e}); requiring checks.", "main-watch")
                self._require = True
                return True
            others = [w for w in flows if w.get("state") == "active"
                      and not (w.get("path") or "").startswith("dynamic/")
                      and w.get("path") != self.cfg.workflow_path]
            self._require = bool(others)
        return self._require

    # ----------------------------------------------------------- verdicts

    def allowed(self, commit: Commit) -> Optional[allowlist.Rule]:
        return allow_match(commit, self.cfg.allow, self.files_for, self.branch)

    def evaluate(self, commits: Sequence[Commit]) -> List[Verdict]:
        """Judge a push. A first pass remembers every PR merged into the branch,
        so a stacked PR's commits can be traced to the PR that carried them."""
        rules = {c.sha: self.allowed(c) for c in commits}
        for c in commits:
            if rules[c.sha]:
                continue
            for p in self.merged_into(self.pulls_for(c.sha), self.branch):
                if all(p.get("number") != q.get("number") for q in self.carriers):
                    self.carriers.append(p)
        return [self.verdict(c, rules[c.sha]) for c in commits]

    def verdict(self, commit: Commit, rule: Optional[allowlist.Rule] = None) -> Verdict:
        rule = rule or self.allowed(commit)
        if rule:
            return Verdict(commit, PASS, f"allowed by {rule.name}")
        pulls = self.pulls_for(commit.sha)
        merged = self.merged_into(pulls, self.branch)
        if merged:
            # Prefer the PR this commit is the merge result of.
            merged.sort(key=lambda p: p.get("merge_commit_sha") != commit.sha)
            return self.verdict_for_pr(commit, merged[0])
        children = [p for p in pulls if p.get("merged_at")]
        carrier = self.find_carrier(commit.sha, children)
        if carrier:
            via = ""
            if children:
                c = children[0]
                via = f"stacked PR #{c['number']} into `{(c.get('base') or {}).get('ref')}`, "
            return self.verdict_for_pr(commit, carrier, via)
        open_prs = [f"#{p['number']}" for p in pulls if not p.get("merged_at")]
        other = [f"#{p['number']} into `{(p.get('base') or {}).get('ref')}`" for p in children]
        extra = ""
        if other:
            extra = (f" (it was merged in {', '.join(other)}, but no PR carrying that branch "
                     f"was merged into `{self.branch}`)")
        elif open_prs:
            extra = f" (it is in unmerged PR {', '.join(open_prs)})"
        return Verdict(commit, FLAG,
                       f"no merged PR into `{self.branch}` contains this commit{extra}")

    def find_carrier(self, sha: str, children: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The PR merged into the branch that brought ``sha`` along.

        A stacked PR (base: another PR's branch) is merged into that branch
        first; its commits reach the default branch when the parent PR is
        merged. Check the PRs this push merged, then walk up each child's
        chain of base branches.
        """
        for pr in self.carriers:
            head = (pr.get("head") or {}).get("sha")
            if head and self.contains(head, sha):
                return pr
        seen: Set[str] = set()
        frontier = [(p.get("base") or {}).get("ref") for p in children]
        for _ in range(MAX_STACK_DEPTH):
            nxt: List[str] = []
            for ref in frontier:
                if not ref or ref in seen or ref == self.branch:
                    continue
                seen.add(ref)
                parents = self.api.get(f"/repos/{self.repo}/pulls",
                                       {"state": "closed", "head": f"{self.owner}:{ref}",
                                        "per_page": 100}) or []
                for p in parents:
                    if not p.get("merged_at"):
                        continue
                    head = (p.get("head") or {}).get("sha")
                    if not head or not self.contains(head, sha):
                        continue
                    base = (p.get("base") or {}).get("ref")
                    if base == self.branch:
                        return p
                    nxt.append(base)
            frontier = nxt
            if not frontier:
                break
        return None

    def verdict_for_pr(self, commit: Commit, pr: Dict[str, Any], via: str = "") -> Verdict:
        result = self.pr_checks(pr)
        number = int(pr["number"])
        reason = f"PR #{number}: {via}{result.detail}"
        state = {GREEN: PASS, RED: FLAG, PENDING: WAIT}[result.state]
        return Verdict(commit, state, reason, number)

    def pr_checks(self, pr: Dict[str, Any]) -> ChecksResult:
        number = int(pr["number"])
        if number not in self._pr_checks:
            head = (pr.get("head") or {}).get("sha")
            if not head:
                self._pr_checks[number] = ChecksResult(RED, "could not find the PR head commit")
            else:
                self._pr_checks[number] = self.head_checks(head, parse_time(pr.get("merged_at")))
        return self._pr_checks[number]

    def workflow_paths(self, sha: str) -> Optional[Dict[Any, str]]:
        """check suite id -> workflow file for the Actions runs on this commit, or
        None when the Actions API cannot be read."""
        try:
            runs = self.api.paginate(f"/repos/{self.repo}/actions/runs", {"head_sha": sha},
                                     item_key="workflow_runs")
        except gh.GitHubError as e:
            if e.status not in (403, 404):
                raise
            gh.annotate("warning", f"could not list the Actions runs on {sha[:7]} ({e}); "
                        "grant `actions: read`. Judging every check suite on its own, so a "
                        "failed run that was re-run green still counts as failed.",
                        "main-watch")
            return None
        return {w["check_suite_id"]: w.get("path") or "" for w in runs
                if w.get("check_suite_id")}

    def head_checks(self, sha: str, merged_at: Optional[dt.datetime] = None) -> ChecksResult:
        """Judge every check on a PR head: GREEN, RED, or PENDING (still running)."""
        # Suites started by pushes to the default branch ran after the merge (a
        # fast-forward merge shares the sha); they are not the PR's gate.
        suites = self.api.paginate(f"/repos/{self.repo}/commits/{sha}/check-suites",
                                   item_key="check_suites")
        post_merge = {s.get("id") for s in suites if s.get("head_branch") == self.branch}
        runs = self.api.paginate(f"/repos/{self.repo}/commits/{sha}/check-runs",
                                 {"filter": "latest"}, item_key="check_runs")
        paths = self.workflow_paths(sha)
        suite_path: Dict[Any, str] = paths or {}
        run_marker = f"/actions/runs/{self.cfg.run_id}/" if self.cfg.run_id else None
        required = set(self.cfg.required_checks)
        bad: List[str] = []
        waiting: List[str] = []
        good = 0
        reported: Set[str] = set()   # check names and status contexts present on the head
        suites_with_runs: Set[Any] = set()

        # `filter=latest` only drops older attempts inside one check suite, and
        # every workflow run is its own suite: a pr-policy run that failed and
        # was re-run green after the body was fixed leaves both on the head.
        # Like GitHub's required checks, only the newest run of each check
        # counts, per workflow file when the Actions API names it.
        newest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for r in runs:
            suite = (r.get("check_suite") or {}).get("id")
            if suite in post_merge:
                continue
            if run_marker and run_marker in (r.get("details_url") or ""):
                continue
            suites_with_runs.add(suite)
            # Unknown workflow (no Actions API, or another app): group by
            # suite when the API failed (strict: an old red run stays red), by
            # app and name otherwise.
            where = suite_path.get(suite) or ("" if paths is not None else f"suite:{suite}")
            key = ((r.get("app") or {}).get("slug") or "", where, r.get("name") or "?")
            if key not in newest or (r.get("id") or 0) > (newest[key].get("id") or 0):
                newest[key] = r

        for r in newest.values():
            name = r.get("name") or "?"
            if name not in required and self.cfg.ignore_checks.matches(name):
                continue
            if r.get("status") != "completed":
                waiting.append(f"{name} is {r.get('status') or 'not started'}")
                reported.add(name)
            elif r.get("conclusion") not in OK_CONCLUSIONS:
                bad.append(f"{name} {r.get('conclusion')}")
                reported.add(name)
            else:
                reported.add(name)
                if r.get("conclusion") == "success" and not POLICY_JOB.search(name):
                    # Skipped and neutral are not failures, but they prove nothing:
                    # a PR whose only job was skipped did not pass anything.
                    good += 1

        newest_suite: Dict[str, Any] = {}
        for s in suites:
            path = suite_path.get(s.get("id"))
            if s.get("id") in post_merge:
                continue
            if path and (s.get("id") or 0) > (newest_suite.get(path) or 0):
                newest_suite[path] = s.get("id")
        for s in suites:
            if s.get("id") in post_merge or s.get("id") in suites_with_runs:
                continue
            path = suite_path.get(s.get("id"))
            if path and newest_suite.get(path) != s.get("id"):
                continue   # a newer run of the same workflow superseded it
            app = (s.get("app") or {}).get("slug") or "an app"
            if s.get("status") == "completed":
                if s.get("conclusion") in BAD_EMPTY_SUITE:
                    bad.append(f"{app} check suite {s.get('conclusion')} before any check ran "
                               "(a workflow that could not start?)")
                elif path and s.get("conclusion") in ("cancelled", "stale"):
                    # The newest run of this workflow on the head, and it never
                    # ran a job: nothing re-ran it, so the workflow did not pass.
                    bad.append(f"{path} was {s.get('conclusion')} before any check ran")
            elif app == "github-actions":
                # A workflow run that has not created its first job yet.
                waiting.append("a GitHub Actions workflow has not started its jobs yet")
            # Other apps register a suite on every push and may never report
            # (the Claude app does exactly that): nothing to wait for.

        try:
            statuses = self.api.paginate(f"/repos/{self.repo}/commits/{sha}/status",
                                         item_key="statuses")
        except gh.GitHubError as e:
            if e.status in (403, 404):
                return ChecksResult(RED, f"could not read commit statuses ({e}); the workflow "
                                    "may need `statuses: read` permission")
            raise
        for s in statuses:
            name = s.get("context") or "?"
            if name == STATUS_CONTEXT:
                continue   # our own mark from an earlier look at this commit
            if name not in required and self.cfg.ignore_checks.matches(name):
                continue
            state = s.get("state")
            if state == "success":
                good += 1
                reported.add(name)
            elif state == "pending":
                waiting.append(f"{name} is pending")
                reported.add(name)
            else:
                bad.append(f"{name} {state}")
                reported.add(name)

        for name in self.cfg.required_checks:
            if name not in reported:
                waiting.append(f"required check `{name}` has not reported")

        if bad:
            return ChecksResult(RED, f"head {sha[:7]} checks not green: {_shown(bad)}")
        if not waiting:
            if good > 0:
                return ChecksResult(GREEN, f"merged with {good} passing check(s) on {sha[:7]}")
            if not self.checks_required():
                return ChecksResult(GREEN, f"merged with no checks on {sha[:7]} "
                                    "(none are required)")
            waiting.append("no checks have reported")
        age = self.cfg.now() - merged_at if merged_at else None
        limit = dt.timedelta(minutes=self.cfg.pending_timeout_minutes)
        if age is None or age >= limit:
            when = (f"{_minutes(age)} minutes after the merge" if age is not None
                    else "and the merge time is unknown")
            if waiting == ["no checks have reported"]:
                return ChecksResult(RED, f"no checks ran on the PR head {sha[:7]} ({when})")
            return ChecksResult(RED, f"head {sha[:7]} checks still not green {when}: "
                                f"{_shown(waiting)}")
        return ChecksResult(PENDING, f"waiting on head {sha[:7]} checks: {_shown(waiting)} "
                            f"(merged {_minutes(age)} min ago; gives up after "
                            f"{self.cfg.pending_timeout_minutes})")


# ------------------------------------------------------------- statuses

def status_description(v: Verdict) -> str:
    """Short text for the commit status. Keeps the ``PR #N:`` prefix the re-check reads."""
    text = f"PR #{v.pr}: checks green" if v.state == PASS and v.pr else v.reason
    return text if len(text) <= 140 else text[:137] + "..."


def publish_statuses(api: gh.GitHub, repo: str, verdicts: Sequence[Verdict],
                     target_url: str) -> List[str]:
    """Mark each judged commit. Returns the errors (empty when all were written)."""
    errors: List[str] = []
    for v in verdicts:
        body = {"state": STATUS_STATE[v.state], "context": STATUS_CONTEXT,
                "description": status_description(v)}
        if target_url:
            body["target_url"] = target_url
        try:
            api.post(f"/repos/{repo}/statuses/{v.commit.sha}", body)
        except gh.GitHubError as e:
            errors.append(f"{v.commit.short}: {e}")
    return errors


def own_status(api: gh.GitHub, repo: str, sha: str) -> Optional[Dict[str, Any]]:
    """The latest ``ci-policy/main-watch`` status on a commit, or None."""
    for s in api.paginate(f"/repos/{repo}/commits/{sha}/status", item_key="statuses"):
        if s.get("context") == STATUS_CONTEXT:
            return s
    return None


# ------------------------------------------------------------- re-check

def recheck(api: gh.GitHub, repo: str, branch: str, watcher: Watcher,
            limit: int = RECHECK_COMMITS) -> Tuple[List[Verdict], List[str]]:
    """Re-judge the commits a push run left pending.

    Reads the branch's ``limit`` newest commits (by position, not date: a
    commit inside a merged PR keeps its old committer date) and re-reads the
    checks of each whose main-watch status is still pending.
    """
    commits = api.get(f"/repos/{repo}/commits", {"sha": branch, "per_page": limit}) or []
    notes: List[str] = []
    verdicts: List[Verdict] = []
    for raw in commits:
        mark = own_status(api, repo, raw["sha"])
        if not mark or mark.get("state") != "pending":
            continue
        commit = commit_from_api(raw)
        m = PR_REF.match(mark.get("description") or "")
        if m:
            pr = api.get(f"/repos/{repo}/pulls/{m.group(1)}")
            if pr.get("merged_at"):
                verdicts.append(watcher.verdict_for_pr(commit, pr))
                continue
        verdicts.append(watcher.verdict(commit))
    if not verdicts:
        notes.append(f"None of the {len(commits)} newest commits on `{branch}` is waiting "
                     "on checks.")
    return verdicts, notes


# ---------------------------------------------------------------- output

def render_issue_body(repo: str, branch: str, event: Dict[str, Any], plan: PushPlan,
                      flagged: List[Verdict], run_url: str, recheck_mode: bool = False) -> str:
    if recheck_mode:
        lines = [f"**main-watch** re-checked commits on `{branch}` whose PR checks were still "
                 "running when the PR was merged, and they did not come out green.", "",
                 f"[Workflow run]({run_url})", ""]
    else:
        pusher = (event.get("pusher") or {}).get("name") or "unknown"
        before = (event.get("before") or ZERO_SHA)[:7]
        after = (event.get("after") or ZERO_SHA)[:7]
        lines = [f"**main-watch** found changes on `{branch}` that did not come through a "
                 "merged PR with passing checks.", "",
                 f"Push `{before}..{after}` by `{pusher}`. [Workflow run]({run_url})", ""]
    for a in plan.alerts:
        lines.append(f"- {a}")
    if plan.alerts:
        lines.append("")
    if flagged:
        lines += ["| Commit | Subject | Why |", "| --- | --- | --- |"]
        for v in flagged:
            lines.append(f"| [{v.commit.short}](https://github.com/{repo}/commit/{v.commit.sha}) "
                         f"| {gh.md_escape(v.commit.subject)} | {gh.md_escape(v.reason)} |")
        lines.append("")
    lines.append("This is an alert only; nothing was reverted. If a class of commits is "
                 "expected here (for example release bumps), add a rule to "
                 "`policy/main-allowlist.json` in drench44/ci-policy (through a PR). Close "
                 "this issue once it has been looked at.")
    return "\n".join(lines)


def raise_alert(api: gh.GitHub, repo: str, label: str, branch: str, body: str) -> str:
    issues = api.paginate(f"/repos/{repo}/issues", {"labels": label, "state": "open"})
    issues = [i for i in issues if "pull_request" not in i]
    if issues:
        issue = sorted(issues, key=lambda i: i["number"])[0]
        api.post(f"/repos/{repo}/issues/{issue['number']}/comments", {"body": body})
        return f"commented on #{issue['number']}"
    try:
        api.post(f"/repos/{repo}/labels", {"name": label, "color": "B60205",
                                           "description": "Commits reached the default branch "
                                                          "without a green PR"})
    except gh.GitHubError as e:
        if e.status != 422:  # 422 = label already exists
            raise
    issue = api.post(f"/repos/{repo}/issues",
                     {"title": f"main-watch: unreviewed changes reached {branch}",
                      "body": body, "labels": [label]})
    return f"opened #{issue['number']}"


RESULT_WORD = {PASS: "PASS", FLAG: "FLAG", WAIT: "WAIT"}


def render_summary(branch: str, plan: PushPlan, verdicts: List[Verdict],
                   alert_result: Optional[str], recheck_mode: bool = False) -> str:
    flagged = [v for v in verdicts if v.state == FLAG]
    waiting = [v for v in verdicts if v.state == WAIT]
    if flagged or plan.alerts:
        headline = "flagged this push" if not recheck_mode else "flagged a re-checked commit"
    elif waiting:
        headline = "is waiting on PR checks that were still running at merge time"
    else:
        headline = "passed"
    title = "main-watch re-check" if recheck_mode else "main-watch"
    lines = [f"## {title} {headline} on `{branch}`", ""]
    for a in plan.alerts:
        lines.append(f"- ALERT: {a}")
    for n in plan.notes:
        lines.append(f"- Note: {n}")
    if plan.alerts or plan.notes:
        lines.append("")
    if verdicts:
        lines += ["| Commit | Subject | Result | Why |", "| --- | --- | --- | --- |"]
        for v in verdicts:
            lines.append(f"| {v.commit.short} | {gh.md_escape(v.commit.subject)} | "
                         f"{RESULT_WORD[v.state]} | {gh.md_escape(v.reason)} |")
    elif not recheck_mode:
        lines.append("No new commits in this push.")
    if waiting:
        lines += ["", "WAIT: the commit got a pending `ci-policy/main-watch` status. The "
                  "scheduled re-check reads the checks again and flags it if they fail or "
                  "are still not done when the timeout runs out."]
    if alert_result:
        lines += ["", f"Alert issue: {alert_result}."]
    return "\n".join(lines)


def load_config() -> Config:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    timeout = gh.env_int("pending-timeout-minutes", 180)
    if timeout < 0:
        raise ValueError("input pending-timeout-minutes: must be 0 or more")
    return Config(
        allow=parse_allow_rules(gh.env_input("allow-rules"), gh.env_input("allowlist-file"),
                                repo),
        ignore_checks=GlobSet(split_list(gh.env_input("ignore-checks"))),
        required_checks=parse_required_checks(gh.env_input("required-checks")),
        require_checks=parse_require_checks(gh.env_input("require-checks", "auto")),
        pending_timeout_minutes=timeout,
        fail_on_alert=gh.env_bool("fail-on-alert", True),
        label=gh.env_input("issue-label", "main-watch"),
        run_id=run_id,
        workflow_path=workflow_path_from_ref(os.environ.get("GITHUB_WORKFLOW_REF", "")),
        set_status=gh.env_bool("set-status", True),
        target_url=f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else "",
    )


def main() -> int:
    api = None
    cfg = None
    event: Dict[str, Any] = {}
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "push")
    recheck_mode = event_name in ("schedule", "workflow_dispatch")
    branch = ""
    try:
        cfg = load_config()
        event = gh.load_event()
        branch = (gh.env_input("branch") or (event.get("repository") or {}).get("default_branch")
                  or "")
        gh_ref = os.environ.get("GITHUB_REF", "")
        if not branch and recheck_mode and gh_ref.startswith("refs/heads/"):
            # A schedule payload may not carry the repository; the run is on
            # the default branch.
            branch = gh_ref[len("refs/heads/"):]
        if not branch:
            raise ValueError("could not tell the default branch; set the `branch` input")
        ref = event.get("ref") or os.environ.get("GITHUB_REF", "")
        if not recheck_mode and ref != f"refs/heads/{branch}":
            protected_elsewhere = ref in ("refs/heads/main", "refs/heads/master")
            gh.annotate("error" if protected_elsewhere else "notice",
                        f"main-watch only watches {branch}; this push was to {ref}."
                        + (" Nothing was checked: fix the workflow's `branch` input."
                           if protected_elsewhere else ""), "main-watch")
            gh.write_summary(f"## main-watch skipped\n\nThis push was to `{ref}`, "
                             f"not `{branch}`.\n")
            # A push to main or master that nobody checked must not look green.
            return 1 if protected_elsewhere else 0
        api = gh.GitHub(gh.env_input("token"),
                        os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        watcher = Watcher(api, repo, branch, cfg)
        if recheck_mode:
            plan = PushPlan([], [], [])
            verdicts, plan.notes = recheck(api, repo, branch, watcher)
        else:
            plan = plan_push(event, api, repo)
            verdicts = watcher.evaluate(plan.commits)
    except Exception as e:  # noqa: BLE001  any failure must be loud, never a quiet pass
        detail = f"{type(e).__name__}: {e}"
        what = "re-check pending commits" if recheck_mode else "check a push"
        gh.annotate("error", f"main-watch could not {what}: {detail}", "main-watch")
        gh.write_summary(f"## main-watch could not run\n\n{gh.md_escape(detail)}\n")
        # A push nobody checked is as bad as a flagged one: open the issue too,
        # when there is enough to do it with.
        try:
            if api is not None and repo:
                span = ("" if recheck_mode else
                        f" (`{str(event.get('before', '?'))[:7]}..."
                        f"{str(event.get('after', '?'))[:7]}`)")
                raise_alert(api, repo, (cfg.label if cfg else "main-watch"),
                            branch or "the default branch",
                            f"**main-watch could not {what}** on `{branch}`{span}, so it is "
                            f"unverified.\n\nError: `{gh.md_escape(detail)[:500]}`\n\nRe-run "
                            "the workflow once the cause is fixed. Nothing was reverted.")
        except Exception as alert_error:  # noqa: BLE001
            gh.annotate("error", f"main-watch also could not open the alert issue: "
                        f"{alert_error}", "main-watch")
        return 1

    flagged = [v for v in verdicts if v.state == FLAG]
    waiting = [v for v in verdicts if v.state == WAIT]
    alert_result = None
    failed = False
    if flagged or plan.alerts:
        run_url = cfg.target_url or f"https://github.com/{repo}/actions"
        body = render_issue_body(repo, branch, event, plan, flagged, run_url, recheck_mode)
        try:
            alert_result = raise_alert(api, repo, cfg.label, branch, body)
        except gh.GitHubError as e:
            gh.annotate("error", f"main-watch flagged this push but could not open or update "
                        f"the alert issue: {e}. Grant `issues: write`.", "main-watch")
            alert_result = f"FAILED to open the issue ({e})"
            failed = True
        for v in flagged:
            gh.annotate("error" if cfg.fail_on_alert else "warning",
                        f"{v.commit.short} {v.commit.subject}: {v.reason}", "main-watch")
        for a in plan.alerts:
            gh.annotate("error" if cfg.fail_on_alert else "warning", a, "main-watch")
    for v in waiting:
        gh.annotate("notice", f"{v.commit.short} {v.commit.subject}: {v.reason}", "main-watch")
    marks = list(verdicts)
    after = str(event.get("after") or ZERO_SHA)
    if plan.alerts and after != ZERO_SHA and all(v.commit.sha != after for v in verdicts):
        # A rewind adds no commits, but the audit still looks for a conclusion
        # on the new tip.
        marks.append(Verdict(Commit(after, "", None, "", ""), FLAG,
                             _shown(plan.alerts, 1)))
    if alert_result and alert_result.startswith("FAILED"):
        # No issue means nobody was told. A final failure status would tell the
        # audit it was handled, so the flagged commits stay pending instead:
        # the scheduled re-check judges them again and retries the issue.
        marks = [Verdict(v.commit, WAIT, v.reason, v.pr) if v.state == FLAG else v
                 for v in marks]
    if cfg.set_status and marks:
        # The status is how the out-of-band audit knows main-watch concluded.
        # Written after the issue, so a flagged commit is never marked before
        # the alert exists.
        errors = publish_statuses(api, repo, marks, cfg.target_url)
        if errors:
            gh.annotate("error", "main-watch could not set its commit status (grant "
                        f"`statuses: write`): {_shown(errors, 3)}", "main-watch")
            plan.notes.append(f"Could not set the {STATUS_CONTEXT} status on "
                              f"{len(errors)} commit(s).")
            failed = True
    gh.write_summary(render_summary(branch, plan, verdicts, alert_result, recheck_mode))
    gh.set_output("result", "alert" if (flagged or plan.alerts)
                  else ("pending" if waiting else "pass"))
    if failed:
        return 1
    return 1 if (flagged or plan.alerts) and cfg.fail_on_alert else 0


if __name__ == "__main__":
    sys.exit(main())
