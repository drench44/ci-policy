"""main-watch: alert when commits reach the default branch without a green PR.

Runs on ``push`` to the default branch. For every commit the push added:

* pass if it matches an allow rule (subject regex, optional author, optional
  "only these paths changed" globs), for example release bumps; or
* pass if it belongs to a PR that was merged into the default branch and
  whose head commit's checks (check runs and commit statuses) all passed;
* otherwise it is flagged.

A force push (the old tip is not an ancestor of the new one) and a deleted
default branch are flagged too. Flags open one issue labeled ``main-watch``,
or add a comment to the one already open. It alerts only; it never reverts.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Sequence, Tuple

from ci_policy import allowlist, gh
from ci_policy.globs import GlobSet, split_list

ZERO_SHA = "0" * 40
OK_CONCLUSIONS = {"success", "neutral", "skipped"}


@dataclass
class Config:
    allow: List[allowlist.Rule] = field(default_factory=list)
    ignore_checks: GlobSet = field(default_factory=lambda: GlobSet([]))
    require_checks: bool = True
    fail_on_alert: bool = True
    label: str = "main-watch"
    run_id: str = ""


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
    ok: bool
    reason: str


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


class Watcher:
    """Evaluates commits against the API, caching per-PR check results."""

    def __init__(self, api: gh.GitHub, repo: str, branch: str, cfg: Config):
        self.api, self.repo, self.branch, self.cfg = api, repo, branch, cfg
        self._pr_checks: Dict[int, Tuple[bool, str]] = {}
        self._files: Dict[str, List[str]] = {}

    def files_for(self, sha: str) -> List[str]:
        if sha not in self._files:
            data = self.api.get(f"/repos/{self.repo}/commits/{sha}")
            files = data.get("files") or []
            if len(files) >= 300:
                # GitHub truncates the list here; an unseen file could break the
                # rule, so a path-limited allow rule must not match.
                self._files[sha] = []
                return self._files[sha]
            self._files[sha] =[f["filename"] for f in files] + \
                [f["previous_filename"] for f in files if f.get("previous_filename")]
        return self._files[sha]

    def verdict(self, commit: Commit) -> Verdict:
        rule = allow_match(commit, self.cfg.allow, self.files_for, self.branch)
        if rule:
            return Verdict(commit, True, f"allowed by {rule.name}")
        pulls = self.api.get(f"/repos/{self.repo}/commits/{commit.sha}/pulls") or []
        merged = [p for p in pulls
                  if p.get("merged_at") and (p.get("base") or {}).get("ref") == self.branch]
        if not merged:
            open_prs = [f"#{p['number']}" for p in pulls if not p.get("merged_at")]
            extra = f" (it is in unmerged PR {', '.join(open_prs)})" if open_prs else ""
            return Verdict(commit, False,
                           f"no merged PR into `{self.branch}` contains this commit{extra}")
        # Prefer the PR this commit is the merge result of.
        merged.sort(key=lambda p: p.get("merge_commit_sha") != commit.sha)
        pr = merged[0]
        ok, why = self.pr_checks(pr)
        if ok:
            return Verdict(commit, True, f"PR #{pr['number']}: {why}")
        return Verdict(commit, False, f"PR #{pr['number']}: {why}")

    def pr_checks(self, pr: Dict[str, Any]) -> Tuple[bool, str]:
        number = int(pr["number"])
        if number not in self._pr_checks:
            head = (pr.get("head") or {}).get("sha")
            if not head:
                self._pr_checks[number] = (False, "could not find the PR head commit")
            else:
                self._pr_checks[number] = self.head_checks(head)
        return self._pr_checks[number]

    def head_checks(self, sha: str) -> Tuple[bool, str]:
        # Suites started by pushes to the default branch ran after the merge (a
        # fast-forward merge shares the sha); they are not the PR's gate.
        suites = self.api.paginate(f"/repos/{self.repo}/commits/{sha}/check-suites",
                                   item_key="check_suites")
        post_merge = {s["id"] for s in suites if s.get("head_branch") == self.branch}
        runs = self.api.paginate(f"/repos/{self.repo}/commits/{sha}/check-runs",
                                 {"filter": "latest"}, item_key="check_runs")
        run_marker = f"/actions/runs/{self.cfg.run_id}/" if self.cfg.run_id else None
        bad: List[str] = []
        good = 0
        for r in runs:
            name = r.get("name") or "?"
            if self.cfg.ignore_checks.matches(name):
                continue
            if (r.get("check_suite") or {}).get("id") in post_merge:
                continue
            if run_marker and run_marker in (r.get("details_url") or ""):
                continue
            if r.get("status") != "completed":
                bad.append(f"{name} is {r.get('status')}")
            elif r.get("conclusion") not in OK_CONCLUSIONS:
                bad.append(f"{name} {r.get('conclusion')}")
            elif r.get("conclusion") == "success":
                # Skipped and neutral are not failures, but they prove nothing:
                # a PR whose only job was skipped did not pass anything.
                good += 1
        try:
            combined = self.api.get(f"/repos/{self.repo}/commits/{sha}/status")
        except gh.GitHubError as e:
            if e.status in (403, 404):
                return False, (f"could not read commit statuses ({e}); the workflow may need "
                               "`statuses: read` permission")
            raise
        for s in combined.get("statuses") or []:
            name = s.get("context") or "?"
            if self.cfg.ignore_checks.matches(name):
                continue
            if s.get("state") == "success":
                good += 1
            else:
                bad.append(f"{name} {s.get('state')}")
        if bad:
            shown = "; ".join(bad[:6]) + (f"; and {len(bad) - 6} more" if len(bad) > 6 else "")
            return False, f"head {sha[:7]} checks not green: {shown}"
        if good == 0 and self.cfg.require_checks:
            return False, f"no checks ran on the PR head {sha[:7]}"
        return True, f"merged with {good} passing check(s) on {sha[:7]}"


def render_issue_body(repo: str, branch: str, event: Dict[str, Any], plan: PushPlan,
                      flagged: List[Verdict], run_url: str) -> str:
    pusher = (event.get("pusher") or {}).get("name") or "unknown"
    before, after = (event.get("before") or ZERO_SHA)[:7], (event.get("after") or ZERO_SHA)[:7]
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


def render_summary(branch: str, plan: PushPlan, verdicts: List[Verdict],
                   alert_result: Optional[str]) -> str:
    flagged = [v for v in verdicts if not v.ok]
    ok = not flagged and not plan.alerts
    lines = [f"## main-watch {'passed' if ok else 'flagged this push'} on `{branch}`", ""]
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
                         f"{'PASS' if v.ok else 'FLAG'} | {gh.md_escape(v.reason)} |")
    else:
        lines.append("No new commits in this push.")
    if alert_result:
        lines += ["", f"Alert issue: {alert_result}."]
    return "\n".join(lines)


def load_config() -> Config:
    return Config(
        allow=parse_allow_rules(gh.env_input("allow-rules"), gh.env_input("allowlist-file"),
                                os.environ.get("GITHUB_REPOSITORY", "")),
        ignore_checks=GlobSet(split_list(gh.env_input("ignore-checks"))),
        require_checks=gh.env_bool("require-checks", True),
        fail_on_alert=gh.env_bool("fail-on-alert", True),
        label=gh.env_input("issue-label", "main-watch"),
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
    )


def main() -> int:
    api = None
    cfg = None
    event = None
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = ""
    try:
        cfg = load_config()
        event = gh.load_event()
        branch = gh.env_input("branch") or (event.get("repository") or {}).get("default_branch")
        if not branch:
            raise ValueError("could not tell the default branch; set the `branch` input")
        ref = event.get("ref") or os.environ.get("GITHUB_REF", "")
        if ref != f"refs/heads/{branch}":
            protected_elsewhere = ref in ("refs/heads/main", "refs/heads/master")
            gh.annotate("warning" if protected_elsewhere else "notice",
                        f"main-watch only watches {branch}; this push was to {ref}."
                        + (" Check the workflow's `branch` input." if protected_elsewhere
                           else ""), "main-watch")
            gh.write_summary(f"## main-watch skipped\n\nThis push was to `{ref}`, "
                             f"not `{branch}`.\n")
            return 0
        api = gh.GitHub(gh.env_input("token"),
                        os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        plan = plan_push(event, api, repo)
        watcher = Watcher(api, repo, branch, cfg)
        verdicts = [watcher.verdict(c) for c in plan.commits]
    except Exception as e:  # noqa: BLE001  any failure must be loud, never a quiet pass
        detail = f"{type(e).__name__}: {e}"
        gh.annotate("error", f"main-watch could not check this push: {detail}", "main-watch")
        gh.write_summary(f"## main-watch could not run\n\n{gh.md_escape(detail)}\n")
        # A push nobody checked is as bad as a flagged one: open the issue too,
        # when there is enough to do it with.
        try:
            if api is not None and repo:
                raise_alert(api, repo, (cfg.label if cfg else "main-watch"),
                            branch or "the default branch",
                            f"**main-watch could not check a push** to `{branch}` "
                            f"(`{(event or {}).get('before', '?')[:7]}..."
                            f"{(event or {}).get('after', '?')[:7]}`), so it is unverified.\n\n"
                            f"Error: `{gh.md_escape(detail)[:500]}`\n\nRe-run the workflow "
                            "once the cause is fixed. Nothing was reverted.")
        except Exception as alert_error:  # noqa: BLE001
            gh.annotate("error", f"main-watch also could not open the alert issue: "
                        f"{alert_error}", "main-watch")
        return 1

    flagged = [v for v in verdicts if not v.ok]
    alert_result = None
    if flagged or plan.alerts:
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
                   f"/actions/runs/{cfg.run_id}")
        body = render_issue_body(repo, branch, event, plan, flagged, run_url)
        try:
            alert_result = raise_alert(api, repo, cfg.label, branch, body)
        except gh.GitHubError as e:
            gh.annotate("error", f"main-watch flagged this push but could not open or update "
                        f"the alert issue: {e}. Grant `issues: write`.", "main-watch")
            alert_result = f"FAILED to open the issue ({e})"
        for v in flagged:
            gh.annotate("error" if cfg.fail_on_alert else "warning",
                        f"{v.commit.short} {v.commit.subject}: {v.reason}", "main-watch")
        for a in plan.alerts:
            gh.annotate("error" if cfg.fail_on_alert else "warning", a, "main-watch")
    gh.write_summary(render_summary(branch, plan, verdicts, alert_result))
    gh.set_output("result", "alert" if (flagged or plan.alerts) else "pass")
    if alert_result and alert_result.startswith("FAILED"):
        return 1
    return 1 if (flagged or plan.alerts) and cfg.fail_on_alert else 0


if __name__ == "__main__":
    sys.exit(main())
