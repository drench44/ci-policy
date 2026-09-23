"""main-watch-audit: notice when main-watch did not run at all.

main-watch runs on each repo's own runner. Private repos use the OMEN, so when
the OMEN is down the job just sits queued (GitHub drops it after a day) and
nobody hears about it. This audit runs somewhere else: a scheduled workflow in
this public repo, on GitHub-hosted runners (free for public repos), reading
every watched repo with a read-only token.

For every push to a watched branch between ``lookback-hours`` and
``grace-hours`` ago, the pushed tip must carry a final ``ci-policy/main-watch``
status (success, failure or error). Also accepted, for callers still pinned to
a main-watch that set no status: a completed main-watch workflow run for that
push. A pending status that old means the scheduled re-check is not running
either. Problems open one issue in this repo labeled ``main-watch-audit`` (or
comment on the open one). Each problem is reported once: the issue carries
the keys it reported, and closing the issue acknowledges them.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from ci_policy import gh
from ci_policy.main_watch import ZERO_SHA, own_status, parse_time, utcnow

FINAL_STATES = {"success", "failure", "error"}
KEYS_RE = re.compile(r"<!-- main-watch-audit keys: ([^>]*) -->")
DEFAULT_WORKFLOW = ".github/workflows/main-watch.yml"
EXPIRY_WARN_DAYS = 14


@dataclass
class Watched:
    repo: str
    branch: str
    workflow: str = DEFAULT_WORKFLOW


@dataclass
class Problem:
    key: str
    repo: str
    text: str


@dataclass
class RepoReport:
    repo: str
    checked: int = 0
    problems: List[Problem] = field(default_factory=list)


def load_repos(path: str) -> List[Watched]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict) or not repos:
        raise ValueError(f"{path}: expected an object with a non-empty `repos` map")
    out = []
    for name, cfg in repos.items():
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
            raise ValueError(f"{path}: `{name}` is not owner/repo")
        if not isinstance(cfg, dict) or not isinstance(cfg.get("branch"), str) \
                or not cfg["branch"]:
            raise ValueError(f"{path}: {name} needs a `branch`")
        workflow = cfg.get("workflow", DEFAULT_WORKFLOW)
        if not isinstance(workflow, str) or not workflow.startswith(".github/workflows/"):
            raise ValueError(f"{path}: {name} `workflow` must be a .github/workflows/ path")
        out.append(Watched(name, cfg["branch"], workflow))
    return out


def _hours(delta: dt.timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.1f} h"


def run_state(api: gh.GitHub, w: Watched, sha: str) -> str:
    """How the main-watch workflow run for this push ended, or why there is none."""
    data = api.get(f"/repos/{w.repo}/actions/runs",
                   {"head_sha": sha, "event": "push", "per_page": 100}) or {}
    runs = [r for r in data.get("workflow_runs") or [] if r.get("path") == w.workflow]
    if not runs:
        return "no main-watch run exists for it"
    latest = max(runs, key=lambda r: (r.get("run_attempt") or 0, r.get("id") or 0))
    if latest.get("status") != "completed":
        return f"its main-watch run is still {latest.get('status')}"
    # success, or failure (what a flag or a loud error produces). Anything
    # else (cancelled, startup_failure when a caller grants too little,
    # action_required, stale) means nothing was judged.
    if latest.get("conclusion") not in ("success", "failure"):
        hint = (" (does the caller grant every permission the pinned main-watch asks for?)"
                if latest.get("conclusion") == "startup_failure" else "")
        return f"its main-watch run ended {latest.get('conclusion') or 'without a conclusion'}{hint}"
    return "concluded"


def audit_repo(api: gh.GitHub, w: Watched, now: dt.datetime, grace_hours: float,
               lookback_hours: float) -> RepoReport:
    report = RepoReport(w.repo)
    oldest = now - dt.timedelta(hours=lookback_hours)
    newest = now - dt.timedelta(hours=grace_hours)
    try:
        activity = api.paginate(f"/repos/{w.repo}/activity",
                                {"ref": f"refs/heads/{w.branch}",
                                 "time_period": "week" if lookback_hours <= 24 * 7 else "month"},
                                limit=1000)
        seen: Set[str] = set()
        for a in activity:
            ts = parse_time(a.get("timestamp"))
            if ts is None or ts < oldest or ts > newest:
                continue
            age = now - ts
            after = a.get("after") or ZERO_SHA
            if a.get("activity_type") == "branch_deletion" or after == ZERO_SHA:
                report.problems.append(Problem(
                    f"{w.repo}@deleted@{a.get('id')}", w.repo,
                    f"`{w.branch}` was deleted {_hours(age)} ago"))
                continue
            if after in seen:
                continue
            seen.add(after)
            report.checked += 1
            mark = own_status(api, w.repo, after)
            state = (mark or {}).get("state")
            if state in FINAL_STATES:
                continue
            short = after[:7]
            link = f"[{short}](https://github.com/{w.repo}/commit/{after})"
            if state == "pending":
                report.problems.append(Problem(
                    f"{w.repo}@{after}", w.repo,
                    f"{link} ({a.get('activity_type')}) is still marked pending "
                    f"{_hours(age)} after the push, so the scheduled re-check is not "
                    f"running: {(mark or {}).get('description') or ''}"))
                continue
            how = run_state(api, w, after)
            if how == "concluded":
                continue
            report.problems.append(Problem(
                f"{w.repo}@{after}", w.repo,
                f"{link} ({a.get('activity_type')}) has no main-watch conclusion "
                f"{_hours(age)} after the push: {how}"))
    except gh.GitHubError as e:
        report.problems.append(Problem(
            f"{w.repo}:read", w.repo,
            f"could not read the repo with the audit token ({e}). The token needs read access "
            "to Metadata, Contents, Commit statuses and Actions on this repo."))
    return report


def token_expiry_problem(api: gh.GitHub, now: dt.datetime) -> Optional[Problem]:
    """Fine-grained tokens report their expiry in a response header."""
    _, _, headers = api.request("GET", "/rate_limit")
    raw = next((v for k, v in headers.items()
                if k.lower() == "github-authentication-token-expiration"), "")
    if not raw:
        return None
    try:
        expires = dt.datetime.strptime(raw.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return Problem("token:expiry-unreadable", "",
                       f"could not read the audit token's expiry `{raw}`")
    if expires - now > dt.timedelta(days=EXPIRY_WARN_DAYS):
        return None
    return Problem(f"token:expires:{expires:%Y-%m-%d}", "",
                   f"the audit token (secret MAIN_WATCH_AUDIT_TOKEN) expires {expires:%Y-%m-%d}; "
                   "make a new one and update the secret")


def reported_keys(api: gh.GitHub, home: str, label: str) -> Tuple[Set[str], Optional[int]]:
    """Keys the newest audit issue (open or closed) already reported, and its number if open."""
    issues = api.get(f"/repos/{home}/issues", {"labels": label, "state": "all",
                                               "sort": "created", "direction": "desc",
                                               "per_page": 10}) or []
    issues = [i for i in issues if "pull_request" not in i]
    if not issues:
        return set(), None
    issue = issues[0]
    texts = [issue.get("body") or ""]
    texts += [c.get("body") or "" for c in
              api.paginate(f"/repos/{home}/issues/{issue['number']}/comments")]
    keys: Set[str] = set()
    for t in texts:
        for m in KEYS_RE.finditer(t):
            keys.update(k for k in m.group(1).split() if k)
    return keys, (issue["number"] if issue.get("state") == "open" else None)


def render_alert(problems: List[Problem], new: Set[str], run_url: str) -> str:
    lines = ["**main-watch-audit** found pushes that main-watch never concluded on. Usually "
             "the runner is down (the OMEN, for private repos) or a main-watch job was "
             "cancelled. Once the runner is back, re-run the main-watch run for that push "
             "from the repo's Actions tab.", "", f"[Audit run]({run_url})", "",
             "| | Repo | Problem |", "| --- | --- | --- |"]
    for p in problems:
        lines.append(f"| {'NEW' if p.key in new else 'still'} | {p.repo or '-'} | "
                     f"{gh.md_escape(p.text)} |")
    lines += ["", "Each problem is reported once. Close this issue to acknowledge them; a "
              "later problem opens a new one.",
              f"<!-- main-watch-audit keys: {' '.join(sorted(p.key for p in problems))} -->"]
    return "\n".join(lines)


def raise_audit_alert(api: gh.GitHub, home: str, label: str, body: str,
                      open_issue: Optional[int]) -> str:
    if open_issue:
        api.post(f"/repos/{home}/issues/{open_issue}/comments", {"body": body})
        return f"commented on #{open_issue}"
    try:
        api.post(f"/repos/{home}/labels", {"name": label, "color": "D93F0B",
                                           "description": "main-watch did not run on a push"})
    except gh.GitHubError as e:
        if e.status != 422:
            raise
    issue = api.post(f"/repos/{home}/issues", {"title": "main-watch-audit: main-watch did not "
                                                        "conclude on some pushes",
                                               "body": body, "labels": [label]})
    return f"opened #{issue['number']}"


def main() -> int:
    home = os.environ.get("GITHUB_REPOSITORY", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{home}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    label = gh.env_input("issue-label", "main-watch-audit")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    now = utcnow()
    problems: List[Problem] = []
    reports: List[RepoReport] = []
    blind = False
    try:
        home_api = gh.GitHub(gh.env_input("token"), api_url)
    except gh.GitHubError as e:
        gh.annotate("error", f"main-watch-audit has no token for this repo: {e}", "audit")
        return 1
    try:
        watched = load_repos(gh.env_input("repos-file"))
        grace = float(gh.env_input("grace-hours", "6"))
        lookback = float(gh.env_input("lookback-hours", "72"))
        if not 0 < grace < lookback:
            raise ValueError("need 0 < grace-hours < lookback-hours")
        token = gh.env_input("audit-token")
        if not token:
            blind = True
            problems.append(Problem("token:missing", "", "the MAIN_WATCH_AUDIT_TOKEN secret is "
                                    "not set, so no repo was audited (README, main-watch "
                                    "audit, has the token recipe)"))
        else:
            api = gh.GitHub(token, api_url)
            try:
                expiry = token_expiry_problem(api, now)
            except gh.GitHubError as e:
                blind = True
                expiry = Problem("token:rejected", "", f"the audit token was rejected ({e})")
            if expiry:
                problems.append(expiry)
            if not blind:
                for w in watched:
                    r = audit_repo(api, w, now, grace, lookback)
                    reports.append(r)
                    problems.extend(r.problems)
                if reports and all(any(p.key.endswith(":read") for p in r.problems)
                                   for r in reports):
                    blind = True
    except Exception as e:  # noqa: BLE001  the audit failing must be loud
        blind = True
        problems.append(Problem(f"audit:crash:{type(e).__name__}", "",
                                f"the audit itself failed: {type(e).__name__}: {e}"))

    lines = [f"## main-watch-audit: {len(problems)} problem(s)", "",
             f"Window: pushes {gh.env_input('lookback-hours', '72')} h to "
             f"{gh.env_input('grace-hours', '6')} h old.", ""]
    if reports:
        lines += ["| Repo | Pushes checked | Problems |", "| --- | --- | --- |"]
        lines += [f"| {r.repo} | {r.checked} | {len(r.problems)} |" for r in reports]
        lines.append("")
    for p in problems:
        lines.append(f"- {p.repo + ': ' if p.repo else ''}{gh.md_escape(p.text)}")

    alert = None
    failed = blind
    if problems:
        try:
            done, open_issue = reported_keys(home_api, home, label)
            new = {p.key for p in problems} - done
            if new:
                alert = raise_audit_alert(home_api, home, label,
                                          render_alert(problems, new, run_url), open_issue)
            else:
                alert = "nothing new; already reported"
        except gh.GitHubError as e:
            alert = f"FAILED to open or update the issue ({e})"
            failed = True
        for p in problems:
            gh.annotate("error" if blind else "warning",
                        f"{p.repo + ': ' if p.repo else ''}{p.text}", "main-watch-audit")
        lines += ["", f"Alert issue: {alert}."]
    gh.write_summary("\n".join(lines))
    gh.set_output("problems", str(len(problems)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
