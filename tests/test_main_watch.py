import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

from fakes import ActionsEnv, FakeGitHub
from ci_policy import gh, main_watch
from ci_policy.globs import GlobSet
from ci_policy.main_watch import Commit, Config, Watcher, parse_allow_rules, plan_push

R = "/repos/drench44/demo"
# Merged one hour before "now": inside the default 180 minute pending timeout.
MERGED = "2026-09-22T00:00:00Z"
NOW = dt.datetime(2026, 9, 22, 1, 0, tzinfo=dt.timezone.utc)
LATE = dt.datetime(2026, 9, 22, 4, 0, tzinfo=dt.timezone.utc)
A, B, C, D = "a" * 40, "b" * 40, "c" * 40, "d" * 40
HEAD = "e" * 40


def api_commit(sha, subject, login="drench44"):
    return {"sha": sha, "commit": {"message": subject + "\n\nbody",
                                   "author": {"name": "Dev", "email": "d@example.com"}},
            "author": {"login": login}}


def event(before=A, after=C, commits=(), **extra):
    e = {"ref": "refs/heads/main", "before": before, "after": after,
         "repository": {"default_branch": "main"}, "pusher": {"name": "drench44"},
         "commits": [{"id": sha, "message": msg, "author": {"username": "drench44",
                                                             "name": "Dev",
                                                             "email": "d@example.com"}}
                     for sha, msg in commits]}
    e.update(extra)
    return e


def merged_pr(number=5, head=HEAD, merge_sha=None, base="main"):
    return {"number": number, "merged_at": "2026-09-22T00:00:00Z", "base": {"ref": base},
            "head": {"sha": head}, "merge_commit_sha": merge_sha}


def green_checks(sha=HEAD, runs=None, statuses=None, suites=None):
    return {
        f"GET {R}/commits/{sha}/check-suites": {"check_suites": suites or [
            {"id": 1, "head_branch": "feature"}]},
        f"GET {R}/commits/{sha}/check-runs": {"check_runs": runs if runs is not None else [
            {"name": "test", "status": "completed", "conclusion": "success",
             "check_suite": {"id": 1}, "details_url": "https://x/actions/runs/1/job/2"}]},
        f"GET {R}/commits/{sha}/status": {"statuses": statuses or []},
    }


def commit(sha=B, subject="Add feature (#5)", login="drench44"):
    return Commit(sha=sha, subject=subject, author_login=login, author_name="Dev",
                  author_email="d@example.com")


class PlanPushTests(unittest.TestCase):
    def test_normal_push_uses_compare(self):
        api = FakeGitHub({f"GET {R}/compare/{A}...{C}": {
            "status": "ahead", "total_commits": 2,
            "commits": [api_commit(B, "one"), api_commit(C, "two")]}})
        plan = plan_push(event(), api, "drench44/demo")
        self.assertEqual([c.sha for c in plan.commits], [B, C])
        self.assertEqual(plan.alerts, [])
        self.assertEqual(plan.commits[0].subject, "one")

    def test_compare_pagination(self):
        def page(params):
            if params.get("page") == 2:
                return {"status": "ahead", "total_commits": 3, "commits": [api_commit(D, "3")]}
            return {"status": "ahead", "total_commits": 3,
                    "commits": [api_commit(B, "1"), api_commit(C, "2")]}
        api = FakeGitHub({f"GET {R}/compare/{A}...{C}": page})
        plan = plan_push(event(), api, "drench44/demo")
        self.assertEqual(len(plan.commits), 3)

    def test_short_compare_is_flagged(self):
        def page(params):
            if params.get("page"):
                return {"commits": []}
            return {"status": "ahead", "total_commits": 5, "commits": [api_commit(B, "1")]}
        api = FakeGitHub({f"GET {R}/compare/{A}...{C}": page})
        plan = plan_push(event(), api, "drench44/demo")
        self.assertTrue(any("1 of 5" in a for a in plan.alerts))

    def test_diverged_without_forced_flag_is_still_a_force_push(self):
        api = FakeGitHub({f"GET {R}/compare/{A}...{C}": {
            "status": "diverged", "total_commits": 1, "commits": [api_commit(C, "rewritten")]}})
        plan = plan_push(event(), api, "drench44/demo")
        self.assertTrue(any("Force push" in a for a in plan.alerts))

    def test_force_push_diverged_is_alert_and_checks_new_commits(self):
        api = FakeGitHub({f"GET {R}/compare/{A}...{C}": {
            "status": "diverged", "total_commits": 1, "commits": [api_commit(C, "rewritten")]}})
        plan = plan_push(event(forced=True), api, "drench44/demo")
        self.assertTrue(any("Force push" in a for a in plan.alerts))
        self.assertEqual([c.sha for c in plan.commits], [C])

    def test_rewind_is_alert(self):
        api = FakeGitHub({f"GET {R}/compare/{A}...{C}": {
            "status": "behind", "total_commits": 0, "commits": []}})
        plan = plan_push(event(), api, "drench44/demo")
        self.assertTrue(plan.alerts)
        self.assertEqual(plan.commits, [])

    def test_old_tip_gone_falls_back_to_event_commits(self):
        api = FakeGitHub({f"GET {R}/compare/{A}...{C}": gh.GitHubError("gone", 404)})
        plan = plan_push(event(commits=[(C, "x")]), api, "drench44/demo")
        self.assertEqual([c.sha for c in plan.commits], [C])
        self.assertTrue(any("no longer exists" in a for a in plan.alerts))

    def test_compare_server_error_raises(self):
        api = FakeGitHub({f"GET {R}/compare/{A}...{C}": gh.GitHubError("down", 502)})
        with self.assertRaises(gh.GitHubError):
            plan_push(event(), api, "drench44/demo")

    def test_first_push_uses_event_commits(self):
        plan = plan_push(event(before="0" * 40, created=True, commits=[(B, "init"), (C, "x")]),
                         FakeGitHub(), "drench44/demo")
        self.assertEqual([c.sha for c in plan.commits], [B, C])
        self.assertEqual(plan.alerts, [])
        self.assertTrue(plan.notes)

    def test_first_push_without_commit_list_uses_head_commit(self):
        e = event(before="0" * 40, commits=[])
        e["head_commit"] = {"id": C, "message": "init", "author": {}}
        plan = plan_push(e, FakeGitHub(), "drench44/demo")
        self.assertEqual([c.sha for c in plan.commits], [C])

    def test_branch_deleted(self):
        plan = plan_push(event(after="0" * 40, deleted=True), FakeGitHub(), "drench44/demo")
        self.assertTrue(any("deleted" in a for a in plan.alerts))


def rules(*items):
    return parse_allow_rules(json.dumps(list(items)))


class AllowRuleTests(unittest.TestCase):
    def test_parse_json(self):
        r = rules({"name": "bump", "subject": "^chore: bump", "paths": "package.json"},
                  {"subject": "^release"})
        self.assertEqual([x.name for x in r], ["bump", "allow-rules[2]"])
        self.assertTrue(r[0].paths.matches("package.json"))

    def test_bad_inputs(self):
        for raw in ["{not json", '{"subject": "x"}', '[{"author": "x"}]',
                    '[{"subject": "(unclosed"}]', '[{"subject": "x", "paths": 3}]']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_allow_rules(raw)

    def test_allowlist_file_rules_for_this_repo_only(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"repos": {"drench44/Demo": [{"name": "rel", "subject": "^release"}],
                                 "drench44/other": [{"subject": "^anything"}]}}, f)
        try:
            r = parse_allow_rules("", f.name, "drench44/demo")
            self.assertEqual([x.name for x in r], ["rel"])
            self.assertEqual(parse_allow_rules("", f.name, "drench44/nope"), [])
        finally:
            os.unlink(f.name)

    def test_shipped_allowlist_is_valid(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "policy", "main-allowlist.json")
        r = parse_allow_rules("", path, "drench44/family-hub")
        self.assertEqual(len(r), 1)

    def watcher(self, rules, files=None):
        routes = {f"GET {R}/commits/{B}": {"files": [{"filename": f} for f in (files or [])]}}
        return Watcher(FakeGitHub(routes), "drench44/demo", "main", Config(allow=rules))

    def test_release_commit_allowed(self):
        r = rules({"name": "release", "subject": r"^release: v\d+\.\d+\.\d+$",
                   "author": "drench44", "paths": ["VERSION", "CHANGELOG.md", "**/index.html"]})
        w = self.watcher(r, ["VERSION", "CHANGELOG.md", "src/web/index.html"])
        v = w.verdict(commit(subject="release: v1.8.0"))
        self.assertTrue(v.ok, v.reason)
        self.assertIn("allowed by release", v.reason)

    def test_release_commit_touching_other_paths_not_allowed(self):
        w = self.watcher(rules({"subject": "^release: v", "paths": ["VERSION"]}),
                         ["VERSION", "src/app.py"])
        w.api.routes[f"GET {R}/commits/{B}/pulls"] = []
        v = w.verdict(commit(subject="release: v1.8.0"))
        self.assertFalse(v.ok)

    def test_wrong_author_not_allowed(self):
        w = self.watcher(rules({"subject": "^release: v", "author": "drench44"}))
        w.api.routes[f"GET {R}/commits/{B}/pulls"] = []
        v = w.verdict(commit(subject="release: v1", login="mallory"))
        self.assertFalse(v.ok)

    def test_author_matches_email_or_name(self):
        r = rules({"subject": "^release", "author": "d@example.com"})
        self.assertTrue(self.watcher(r).verdict(commit(subject="release", login=None)).ok)

    def test_rule_for_other_branch_does_not_apply(self):
        w = self.watcher(rules({"subject": "^release", "branch": "master"}))
        w.api.routes[f"GET {R}/commits/{B}/pulls"] = []
        self.assertFalse(w.verdict(commit(subject="release")).ok)

    def test_truncated_file_list_never_matches_paths_rule(self):
        w = self.watcher(rules({"subject": "^release", "paths": ["**"]}),
                         [f"f{i}" for i in range(300)])
        w.api.routes[f"GET {R}/commits/{B}/pulls"] = []
        self.assertFalse(w.verdict(commit(subject="release")).ok)


class VerdictTests(unittest.TestCase):
    def watcher(self, routes, **cfg):
        cfg.setdefault("run_id", "999")
        return Watcher(FakeGitHub(routes), "drench44/demo", "main", Config(**cfg))

    def test_merged_pr_with_green_checks_passes(self):
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()], **green_checks()}
        v = self.watcher(routes).verdict(commit())
        self.assertTrue(v.ok, v.reason)
        self.assertIn("PR #5", v.reason)

    def test_no_pr_is_flagged(self):
        v = self.watcher({f"GET {R}/commits/{B}/pulls": []}).verdict(commit())
        self.assertFalse(v.ok)
        self.assertIn("no merged PR", v.reason)

    def test_open_pr_only_is_flagged_and_named(self):
        pr = {"number": 9, "merged_at": None, "base": {"ref": "main"}, "head": {"sha": HEAD}}
        v = self.watcher({f"GET {R}/commits/{B}/pulls": [pr]}).verdict(commit())
        self.assertFalse(v.ok)
        self.assertIn("#9", v.reason)

    def test_pr_merged_into_other_branch_is_flagged(self):
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr(base="develop")], **green_checks(),
                  f"GET {R}/pulls": []}
        v = self.watcher(routes).verdict(commit())
        self.assertEqual(v.state, "flag")
        self.assertIn("#5 into `develop`", v.reason)

    def test_failing_check_is_flagged(self):
        runs = [{"name": "test", "status": "completed", "conclusion": "failure",
                 "check_suite": {"id": 1}}]
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()], **green_checks(runs=runs)}
        v = self.watcher(routes).verdict(commit())
        self.assertFalse(v.ok)
        self.assertIn("test failure", v.reason)

    def test_pending_check_is_flagged(self):
        runs = [{"name": "build", "status": "in_progress", "conclusion": None,
                 "check_suite": {"id": 1}}]
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()], **green_checks(runs=runs)}
        v = self.watcher(routes).verdict(commit())
        self.assertFalse(v.ok)
        self.assertIn("in_progress", v.reason)

    def test_skipped_and_neutral_are_ok(self):
        runs = [{"name": n, "status": "completed", "conclusion": c, "check_suite": {"id": 1}}
                for n, c in [("a", "success"), ("b", "skipped"), ("c", "neutral")]]
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()], **green_checks(runs=runs)}
        v = self.watcher(routes).verdict(commit())
        self.assertTrue(v.ok)
        self.assertIn("1 passing check", v.reason)

    def test_only_skipped_checks_prove_nothing(self):
        runs = [{"name": n, "status": "completed", "conclusion": c, "check_suite": {"id": 1}}
                for n, c in [("b", "skipped"), ("c", "neutral")]]
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()], **green_checks(runs=runs)}
        v = self.watcher(routes).verdict(commit())
        self.assertFalse(v.ok)
        self.assertIn("no checks", v.reason)

    def test_no_checks_flagged_unless_not_required(self):
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()], **green_checks(runs=[])}
        v = self.watcher(routes).verdict(commit())
        self.assertFalse(v.ok)
        self.assertIn("no checks", v.reason)
        self.assertTrue(self.watcher(routes, require_checks=False).verdict(commit()).ok)

    def test_failed_status_is_flagged(self):
        statuses = [{"context": "Vercel", "state": "failure"}]
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()],
                  **green_checks(statuses=statuses)}
        v = self.watcher(routes).verdict(commit())
        self.assertFalse(v.ok)
        self.assertIn("Vercel failure", v.reason)

    def test_pending_status_is_flagged_but_can_be_ignored(self):
        statuses = [{"context": "Vercel Preview", "state": "pending"}]
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()],
                  **green_checks(statuses=statuses)}
        self.assertFalse(self.watcher(routes).verdict(commit()).ok)
        w = self.watcher(routes, ignore_checks=GlobSet(["Vercel*"]))
        self.assertTrue(w.verdict(commit()).ok)

    def test_statuses_forbidden_is_flagged_with_fix(self):
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()], **green_checks()}
        routes[f"GET {R}/commits/{HEAD}/status"] = gh.GitHubError("forbidden", 403)
        v = self.watcher(routes).verdict(commit())
        self.assertFalse(v.ok)
        self.assertIn("statuses: read", v.reason)

    def test_post_merge_suite_and_own_run_are_ignored(self):
        # Fast-forward merge: the PR head IS the pushed commit, so main's own
        # push-triggered runs (still in progress) sit on the same sha.
        runs = [
            {"name": "test", "status": "completed", "conclusion": "success",
             "check_suite": {"id": 1}},
            {"name": "ci on main", "status": "in_progress", "check_suite": {"id": 2}},
            {"name": "main-watch", "status": "in_progress", "check_suite": {"id": 3},
             "details_url": "https://github.com/x/actions/runs/999/job/1"},
        ]
        suites = [{"id": 1, "head_branch": "feature"}, {"id": 2, "head_branch": "main"},
                  {"id": 3, "head_branch": "feature"}]
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr(head=B)],
                  **green_checks(sha=B, runs=runs, suites=suites)}
        v = self.watcher(routes).verdict(commit())
        self.assertTrue(v.ok, v.reason)

    def test_pr_checks_cached_per_pr(self):
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()],
                  f"GET {R}/commits/{C}/pulls": [merged_pr()], **green_checks()}
        w = self.watcher(routes)
        w.verdict(commit(B))
        w.verdict(commit(C))
        runs_calls = [c for c in w.api.calls if c[1].endswith("/check-runs")]
        self.assertEqual(len(runs_calls), 1)

    def test_prefers_pr_whose_merge_commit_is_this_commit(self):
        other = merged_pr(number=4, head="f" * 40)
        mine = merged_pr(number=5, merge_sha=B)
        routes = {f"GET {R}/commits/{B}/pulls": [other, mine], **green_checks()}
        v = self.watcher(routes).verdict(commit())
        self.assertIn("PR #5", v.reason)


_ids = iter(range(1000, 10 ** 6))


def run(name, status="completed", conclusion="success", suite=1, **extra):
    # Check run ids grow with time, like GitHub's; later calls are newer runs.
    r = {"id": next(_ids), "name": name, "status": status,
         "conclusion": conclusion if status == "completed" else None, "check_suite": {"id": suite},
         "app": {"slug": "github-actions"}}
    r.update(extra)
    return r


def suite(id_, app="github-actions", status="completed", conclusion="success",
          branch="feature"):
    return {"id": id_, "app": {"slug": app}, "status": status, "conclusion": conclusion,
            "head_branch": branch}


class JudgeHelpers(unittest.TestCase):
    def watcher(self, routes, now=NOW, **cfg):
        cfg.setdefault("run_id", "999")
        return Watcher(FakeGitHub(routes), "drench44/demo", "main",
                       Config(now=lambda: now, **cfg))

    def judge(self, runs=None, statuses=None, suites=None, now=NOW, extra_routes=None, **cfg):
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr()],
                  **green_checks(runs=runs, statuses=statuses, suites=suites),
                  **(extra_routes or {})}
        return self.watcher(routes, now=now, **cfg).verdict(commit())


class AllGreenTests(JudgeHelpers):
    """Every check on the head must be green, not just one of them."""

    def test_one_pass_does_not_cover_a_failure(self):
        v = self.judge(runs=[run("lint"), run("typecheck"), run("test", conclusion="failure")],
                       statuses=[{"context": "Vercel", "state": "success"}])
        self.assertEqual(v.state, "flag")
        self.assertIn("test failure", v.reason)

    def test_every_bad_conclusion_is_red(self):
        for c in ["failure", "cancelled", "timed_out", "action_required", "stale",
                  "startup_failure"]:
            with self.subTest(conclusion=c):
                v = self.judge(runs=[run("ok"), run("x", conclusion=c)])
                self.assertEqual(v.state, "flag")

    def test_error_status_is_red_even_with_green_runs(self):
        v = self.judge(statuses=[{"context": "ci/legacy", "state": "error"}])
        self.assertEqual(v.state, "flag")
        self.assertIn("ci/legacy error", v.reason)

    def test_newest_run_of_a_check_counts_failure_after_success(self):
        runs = [run("test", suite=1), run("test", conclusion="failure", suite=2)]
        v = self.judge(runs=runs, suites=[suite(1), suite(2)])
        self.assertEqual(v.state, "flag")

    POLICY_RUNS = {f"GET {R}/actions/runs": {"workflow_runs": [
        {"check_suite_id": 1, "path": ".github/workflows/pr-policy.yml"},
        {"check_suite_id": 2, "path": ".github/workflows/pr-policy.yml"},
        {"check_suite_id": 3, "path": ".github/workflows/pr-policy.yml"},
        {"check_suite_id": 4, "path": ".github/workflows/ci.yml"}]}}

    def test_failed_run_rerun_green_on_the_same_head_is_green(self):
        # pr-policy failed, the body was fixed, the `edited` run passed: three
        # suites on one head (seen live on cpapclarity).
        runs = [run("policy / pr-policy", conclusion="failure", suite=1),
                run("policy / pr-policy", conclusion="failure", suite=2),
                run("policy / pr-policy", suite=3), run("test", suite=4)]
        v = self.judge(runs=runs, suites=[suite(i) for i in (1, 2, 3, 4)],
                       extra_routes=self.POLICY_RUNS)
        self.assertEqual(v.state, "pass", v.reason)

    def test_rerun_still_running_waits(self):
        runs = [run("policy / pr-policy", conclusion="failure", suite=1),
                run("policy / pr-policy", status="queued", suite=2), run("test", suite=4)]
        v = self.judge(runs=runs, suites=[suite(1), suite(2), suite(4)],
                       extra_routes=self.POLICY_RUNS)
        self.assertEqual(v.state, "pending")

    def test_without_the_actions_api_every_suite_counts_on_its_own(self):
        # Strict fallback: a red run in an older suite stays red, and it says why.
        runs = [run("policy / pr-policy", conclusion="failure", suite=1),
                run("policy / pr-policy", suite=3), run("test", suite=4)]
        with mock.patch.object(gh, "annotate") as annotate:
            v = self.judge(runs=runs, suites=[suite(1), suite(3), suite(4)],
                           extra_routes={f"GET {R}/actions/runs": gh.GitHubError("no", 403)})
        self.assertEqual(v.state, "flag")
        self.assertIn("actions: read", annotate.call_args[0][1])

    def test_policy_jobs_alone_do_not_count_as_the_prs_checks(self):
        # pr-policy runs on every PR; a PR whose CI never ran must not pass on it.
        runs = [run("policy / pr-policy"), run("watch / main-watch")]
        self.assertEqual(self.judge(runs=runs).state, "pending")
        v = self.judge(runs=runs, now=LATE)
        self.assertEqual(v.state, "flag")
        self.assertIn("no checks ran", v.reason)

    def test_policy_job_failure_still_counts(self):
        v = self.judge(runs=[run("test"), run("policy / pr-policy", conclusion="failure")])
        self.assertEqual(v.state, "flag")

    def test_newest_run_of_a_workflow_cancelled_before_any_job_is_red(self):
        wf = {f"GET {R}/actions/runs": {"workflow_runs": [
            {"check_suite_id": 1, "path": ".github/workflows/ci.yml"},
            {"check_suite_id": 2, "path": ".github/workflows/lint.yml"}]}}
        v = self.judge(runs=[run("test", suite=1)],
                       suites=[suite(1), suite(2, conclusion="cancelled")], extra_routes=wf)
        self.assertEqual(v.state, "flag")
        self.assertIn(".github/workflows/lint.yml was cancelled", v.reason)

    def test_post_merge_suite_does_not_supersede_a_failed_pr_suite(self):
        # Fast-forward: main's push run of ci.yml shares the sha and is newer.
        wf = {f"GET {R}/actions/runs": {"workflow_runs": [
            {"check_suite_id": 1, "path": ".github/workflows/ci.yml"},
            {"check_suite_id": 2, "path": ".github/workflows/ci.yml"},
            {"check_suite_id": 3, "path": ".github/workflows/other.yml"}]}}
        v = self.judge(runs=[run("x", suite=3)],
                       suites=[suite(1, conclusion="startup_failure"),
                               suite(2, branch="main"), suite(3)], extra_routes=wf)
        self.assertEqual(v.state, "flag")

    def test_same_job_name_in_two_workflows_counts_twice(self):
        runs = [run("test", conclusion="failure", suite=1), run("test", suite=2)]
        wf = {f"GET {R}/actions/runs": {"workflow_runs": [
            {"check_suite_id": 1, "path": ".github/workflows/a.yml"},
            {"check_suite_id": 2, "path": ".github/workflows/b.yml"}]}}
        v = self.judge(runs=runs, suites=[suite(1), suite(2)], extra_routes=wf)
        self.assertEqual(v.state, "flag")

    def test_superseded_empty_suite_of_the_same_workflow_is_ignored(self):
        wf = {f"GET {R}/actions/runs": {"workflow_runs": [
            {"check_suite_id": 1, "path": ".github/workflows/ci.yml"},
            {"check_suite_id": 2, "path": ".github/workflows/ci.yml"}]}}
        v = self.judge(runs=[run("test", suite=2)],
                       suites=[suite(1, conclusion="failure"), suite(2)], extra_routes=wf)
        self.assertEqual(v.state, "pass", v.reason)

    def test_actions_api_error_other_than_forbidden_is_raised(self):
        with self.assertRaises(gh.GitHubError):
            self.judge(extra_routes={f"GET {R}/actions/runs": gh.GitHubError("down", 502)})

    def test_workflow_that_could_not_start_is_red(self):
        suites = [suite(1), suite(2, conclusion="startup_failure")]
        v = self.judge(runs=[run("test")], suites=suites)
        self.assertEqual(v.state, "flag")
        self.assertIn("startup_failure before any check ran", v.reason)

    def test_empty_failed_suite_from_any_app_is_red(self):
        v = self.judge(runs=[run("test")],
                       suites=[suite(1), suite(2, app="vercel", conclusion="failure")])
        self.assertEqual(v.state, "flag")

    def test_suite_cancelled_before_it_started_is_ignored(self):
        # Concurrency cancels a queued run when a newer one on the same commit starts.
        v = self.judge(runs=[run("test")], suites=[suite(1), suite(2, conclusion="cancelled")])
        self.assertEqual(v.state, "pass", v.reason)

    def test_app_that_never_reports_is_ignored(self):
        # Seen on every drench44 PR: the Claude app's suite stays queued with no runs.
        v = self.judge(runs=[run("test")],
                       suites=[suite(1), suite(2, app="claude", status="queued",
                                                 conclusion=None)])
        self.assertEqual(v.state, "pass", v.reason)

    def test_actions_workflow_without_jobs_yet_is_pending(self):
        v = self.judge(runs=[run("test")],
                       suites=[suite(1), suite(2, status="queued", conclusion=None)])
        self.assertEqual(v.state, "pending")
        self.assertIn("has not started its jobs", v.reason)

    def test_own_status_on_the_head_is_ignored(self):
        v = self.judge(statuses=[{"context": "ci-policy/main-watch", "state": "pending"}])
        self.assertEqual(v.state, "pass", v.reason)


class PendingAtMergeTests(JudgeHelpers):
    """Automation that merges without waiting: wait, then decide."""

    def test_check_still_running_waits(self):
        v = self.judge(runs=[run("lint"), run("Vitest", status="queued")])
        self.assertEqual(v.state, "pending")
        self.assertIn("Vitest is queued", v.reason)
        self.assertIn("merged 60 min ago", v.reason)
        self.assertEqual(v.pr, 5)

    def test_pending_status_waits(self):
        v = self.judge(statuses=[{"context": "Vercel", "state": "pending"}])
        self.assertEqual(v.state, "pending")

    def test_still_running_after_the_timeout_is_flagged(self):
        v = self.judge(runs=[run("lint"), run("Vitest", status="in_progress")], now=LATE)
        self.assertEqual(v.state, "flag")
        self.assertIn("still not green 240 minutes after the merge", v.reason)
        self.assertIn("Vitest is in_progress", v.reason)

    def test_timeout_is_configurable(self):
        runs = [run("lint"), run("Vitest", status="in_progress")]
        self.assertEqual(self.judge(runs=runs, pending_timeout_minutes=30).state, "flag")
        self.assertEqual(self.judge(runs=runs, now=LATE,
                                    pending_timeout_minutes=300).state, "pending")

    def test_failure_does_not_wait_for_the_rest(self):
        v = self.judge(runs=[run("lint", conclusion="failure"), run("Vitest", status="queued")])
        self.assertEqual(v.state, "flag")

    def test_no_checks_yet_waits_then_flags(self):
        self.assertEqual(self.judge(runs=[]).state, "pending")
        v = self.judge(runs=[], now=LATE)
        self.assertEqual(v.state, "flag")
        self.assertIn("no checks ran on the PR head", v.reason)

    def test_unknown_merge_time_does_not_wait(self):
        routes = {f"GET {R}/commits/{B}/pulls": [dict(merged_pr(), merged_at="garbage")],
                  **green_checks(runs=[run("Vitest", status="queued")])}
        v = self.watcher(routes).verdict(commit())
        self.assertEqual(v.state, "flag")
        self.assertIn("merge time is unknown", v.reason)


class RequiredChecksTests(JudgeHelpers):
    def test_required_check_present_and_green_passes(self):
        v = self.judge(runs=[run("Vitest (full suite)"), run("lint")],
                       required_checks=["Vitest (full suite)"])
        self.assertEqual(v.state, "pass", v.reason)

    def test_required_status_context_counts(self):
        v = self.judge(statuses=[{"context": "Vercel", "state": "success"}],
                       required_checks=["Vercel"])
        self.assertEqual(v.state, "pass", v.reason)

    def test_missing_required_check_is_not_green(self):
        v = self.judge(runs=[run("lint")], required_checks=["Vitest (full suite)"])
        self.assertEqual(v.state, "pending")
        self.assertIn("required check `Vitest (full suite)` has not reported", v.reason)
        v = self.judge(runs=[run("lint")], required_checks=["Vitest (full suite)"], now=LATE)
        self.assertEqual(v.state, "flag")
        self.assertIn("Vitest (full suite)", v.reason)

    def test_required_check_skipped_counts_as_green(self):
        # GitHub treats a skipped required check as passing.
        v = self.judge(runs=[run("lint"), run("Vitest", conclusion="skipped")],
                       required_checks=["Vitest"])
        self.assertEqual(v.state, "pass", v.reason)

    def test_required_beats_ignore(self):
        v = self.judge(runs=[run("lint"), run("Vitest", conclusion="failure")],
                       required_checks=["Vitest"], ignore_checks=GlobSet(["Vitest"]))
        self.assertEqual(v.state, "flag")

    def test_required_check_from_post_merge_suite_does_not_count(self):
        # Fast-forward: main's own run of the check sits on the same sha.
        runs = [run("lint", suite=1), run("Vitest", suite=2)]
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr(head=B)],
                  **green_checks(sha=B, runs=runs,
                                 suites=[suite(1), suite(2, branch="main")])}
        v = self.watcher(routes, required_checks=["Vitest"]).verdict(commit())
        self.assertEqual(v.state, "pending")


class RequireChecksAutoTests(JudgeHelpers):
    WF = f"GET {R}/actions/workflows"

    def flows(self, *paths, state="active"):
        return {self.WF: {"workflows": [{"path": p, "state": state} for p in paths]}}

    def test_repo_with_other_workflows_needs_a_check(self):
        v = self.judge(runs=[], now=LATE, require_checks=None,
                       workflow_path=".github/workflows/main-watch.yml",
                       extra_routes=self.flows(".github/workflows/main-watch.yml",
                                               ".github/workflows/ci.yml"))
        self.assertEqual(v.state, "flag")

    def test_repo_whose_only_workflow_is_main_watch_needs_none(self):
        v = self.judge(runs=[], now=LATE, require_checks=None,
                       workflow_path=".github/workflows/main-watch.yml",
                       extra_routes=self.flows(".github/workflows/main-watch.yml",
                                               "dynamic/dependabot/dependabot-updates"))
        self.assertEqual(v.state, "pass", v.reason)
        self.assertIn("none are required", v.reason)

    def test_disabled_workflows_do_not_count(self):
        v = self.judge(runs=[], now=LATE, require_checks=None,
                       extra_routes=self.flows(".github/workflows/old.yml",
                                               state="disabled_manually"))
        self.assertEqual(v.state, "pass", v.reason)

    def test_unreadable_workflow_list_means_required(self):
        with mock.patch("builtins.print"):
            v = self.judge(runs=[], now=LATE, require_checks=None,
                           extra_routes={self.WF: gh.GitHubError("forbidden", 403)})
        self.assertEqual(v.state, "flag")

    def test_skipped_only_still_proves_nothing_when_required(self):
        v = self.judge(runs=[run("a", conclusion="skipped")], now=LATE, require_checks=None,
                       extra_routes=self.flows(".github/workflows/ci.yml"))
        self.assertEqual(v.state, "flag")


H_A, H_B, X, M = "1" * 40, "2" * 40, "3" * 40, "4" * 40


def pr(number, head, base, merged=True, merge_sha=None):
    return {"number": number, "merged_at": MERGED if merged else None, "base": {"ref": base},
            "head": {"sha": head}, "merge_commit_sha": merge_sha}


class StackedPRTests(JudgeHelpers):
    """PR #7 (branch feat-b, base feat-a) merged into feat-a; PR #6 (feat-a) into main."""

    def routes(self, a_checks=None, compare="ahead"):
        return {
            # X is a commit of the child PR; M is PR #6's merge commit.
            f"GET {R}/commits/{X}/pulls": [pr(7, H_B, "feat-a")],
            f"GET {R}/commits/{M}/pulls": [pr(6, H_A, "main", merge_sha=M)],
            f"GET {R}/compare/{X}...{H_A}": {"status": compare},
            **(a_checks or green_checks(sha=H_A)),
        }

    def test_child_commit_reached_main_inside_the_parent_pr(self):
        w = self.watcher(self.routes())
        verdicts = w.evaluate([commit(X, "child work"), commit(M, "Merge pull request #6")])
        self.assertEqual([v.state for v in verdicts], ["pass", "pass"], verdicts)
        self.assertIn("PR #6: stacked PR #7 into `feat-a`", verdicts[0].reason)
        self.assertEqual(verdicts[0].pr, 6)

    def test_parent_checks_red_flags_the_child_commit(self):
        runs = [run("test", conclusion="failure")]
        w = self.watcher(self.routes(a_checks=green_checks(sha=H_A, runs=runs)))
        verdicts = w.evaluate([commit(X), commit(M)])
        self.assertEqual([v.state for v in verdicts], ["flag", "flag"])
        self.assertIn("stacked PR #7", verdicts[0].reason)

    def test_found_by_walking_the_base_branch_without_the_push(self):
        # A re-check sees the child commit alone: find PR #6 from feat-a.
        routes = self.routes()
        routes[f"GET {R}/pulls"] = lambda params: (
            [pr(6, H_A, "main")] if params["head"] == "drench44:feat-a" else [])
        v = self.watcher(routes).verdict(commit(X))
        self.assertEqual(v.state, "pass", v.reason)
        self.assertIn("PR #6", v.reason)

    def test_two_level_stack(self):
        # #8 (feat-c -> feat-b), #7 (feat-b -> feat-a), #6 (feat-a -> main).
        routes = {f"GET {R}/commits/{X}/pulls": [pr(8, "5" * 40, "feat-b")],
                  f"GET {R}/pulls": lambda params: {
                      "drench44:feat-b": [pr(7, H_B, "feat-a")],
                      "drench44:feat-a": [pr(6, H_A, "main")]}.get(params["head"], []),
                  f"GET {R}/compare/{X}...{H_B}": {"status": "ahead"},
                  f"GET {R}/compare/{X}...{H_A}": {"status": "ahead"},
                  **green_checks(sha=H_A)}
        v = self.watcher(routes).verdict(commit(X))
        self.assertEqual(v.state, "pass", v.reason)
        self.assertIn("PR #6", v.reason)

    def test_parent_pr_that_does_not_contain_the_commit_is_not_a_carrier(self):
        routes = self.routes(compare="diverged")
        routes[f"GET {R}/pulls"] = [pr(6, H_A, "main")]
        w = self.watcher(routes)
        verdicts = w.evaluate([commit(X), commit(M)])
        self.assertEqual(verdicts[0].state, "flag")
        self.assertIn("merged in #7 into `feat-a`", verdicts[0].reason)

    def test_parent_merged_somewhere_else_is_flagged(self):
        routes = {f"GET {R}/commits/{X}/pulls": [pr(7, H_B, "feat-a")],
                  f"GET {R}/pulls": lambda params: {
                      "drench44:feat-a": [pr(6, H_A, "develop")]}.get(params["head"], []),
                  f"GET {R}/compare/{X}...{H_A}": {"status": "ahead"}}
        v = self.watcher(routes).verdict(commit(X))
        self.assertEqual(v.state, "flag")

    def test_unmerged_parent_is_not_a_carrier(self):
        routes = {f"GET {R}/commits/{X}/pulls": [pr(7, H_B, "feat-a")],
                  f"GET {R}/pulls": [pr(6, H_A, "main", merged=False)]}
        v = self.watcher(routes).verdict(commit(X))
        self.assertEqual(v.state, "flag")

    def test_stack_cycle_terminates(self):
        routes = {f"GET {R}/commits/{X}/pulls": [pr(7, H_B, "feat-a")],
                  f"GET {R}/pulls": lambda params: {
                      "drench44:feat-a": [pr(6, H_A, "feat-b")],
                      "drench44:feat-b": [pr(5, "6" * 40, "feat-a")]}.get(params["head"], []),
                  f"GET {R}/compare/{X}...{H_A}": {"status": "ahead"},
                  f"GET {R}/compare/{X}...{'6' * 40}": {"status": "ahead"}}
        v = self.watcher(routes).verdict(commit(X))
        self.assertEqual(v.state, "flag")


class ParseTests(unittest.TestCase):
    def test_required_checks_one_per_line(self):
        self.assertEqual(main_watch.parse_required_checks(
            "Vitest (full suite)\n  # comment\n\nBuild, lint and test\nVitest (full suite)\n"),
            ["Vitest (full suite)", "Build, lint and test"])
        self.assertEqual(main_watch.parse_required_checks(""), [])

    def test_require_checks(self):
        self.assertIsNone(main_watch.parse_require_checks(""))
        self.assertIsNone(main_watch.parse_require_checks("auto"))
        self.assertTrue(main_watch.parse_require_checks("true"))
        self.assertFalse(main_watch.parse_require_checks("false"))
        with self.assertRaises(ValueError):
            main_watch.parse_require_checks("sometimes")

    def test_workflow_path_from_ref(self):
        self.assertEqual(main_watch.workflow_path_from_ref(
            "drench44/demo/.github/workflows/main-watch.yml@refs/heads/main"),
            ".github/workflows/main-watch.yml")
        self.assertEqual(main_watch.workflow_path_from_ref(""), "")

    def test_status_description_keeps_pr_prefix_and_fits(self):
        v = main_watch.Verdict(commit(), "pending", "PR #12: " + "x" * 300, 12)
        d = main_watch.status_description(v)
        self.assertEqual(len(d), 140)
        self.assertTrue(main_watch.PR_REF.match(d))
        v = main_watch.Verdict(commit(), "pass", "PR #12: merged with 3 passing", 12)
        self.assertEqual(main_watch.status_description(v), "PR #12: checks green")
        v = main_watch.Verdict(commit(), "pass", "allowed by release script")
        self.assertEqual(main_watch.status_description(v), "allowed by release script")


class AlertTests(unittest.TestCase):
    def test_comments_on_open_issue(self):
        api = FakeGitHub({f"GET {R}/issues": [{"number": 3}, {"number": 2},
                                              {"number": 1, "pull_request": {}}],
                          f"POST {R}/issues/2/comments": {"id": 1}})
        self.assertEqual(main_watch.raise_alert(api, "drench44/demo", "main-watch", "main", "b"),
                         "commented on #2")

    def test_opens_issue_and_label(self):
        api = FakeGitHub({f"GET {R}/issues": [],
                          f"POST {R}/labels": gh.GitHubError("exists", 422),
                          f"POST {R}/issues": {"number": 11}})
        self.assertEqual(main_watch.raise_alert(api, "drench44/demo", "main-watch", "main", "b"),
                         "opened #11")
        path, body = api.posts[-1]
        self.assertEqual(body["labels"], ["main-watch"])

    def test_label_error_other_than_exists_raises(self):
        api = FakeGitHub({f"GET {R}/issues": [],
                          f"POST {R}/labels": gh.GitHubError("forbidden", 403)})
        with self.assertRaises(gh.GitHubError):
            main_watch.raise_alert(api, "drench44/demo", "main-watch", "main", "b")


class MainTests(unittest.TestCase):
    def run_main(self, routes, ev, inputs=None, repo="drench44/demo", extra=None):
        routes = dict(routes)
        routes.setdefault(f"POST /repos/{repo}/statuses/*", {})
        fake = FakeGitHub(routes)
        env_extra = {"GITHUB_REPOSITORY": repo, **(extra or {})}
        with ActionsEnv(ev, inputs, env_extra) as env, \
                mock.patch.object(gh, "GitHub", return_value=fake), \
                mock.patch("builtins.print"):
            code = main_watch.main()
            return code, env.summary(), env.outputs(), fake

    @staticmethod
    def issue_bodies(fake):
        return [b["body"] for p, b in fake.posts if "/statuses/" not in p and "body" in b]

    @staticmethod
    def statuses(fake):
        return {p.rsplit("/", 1)[1]: b for p, b in fake.posts if "/statuses/" in p}

    def compare(self, *commits, status="ahead"):
        return {f"GET {R}/compare/{A}...{C}": {"status": status, "total_commits": len(commits),
                                                "commits": list(commits)}}

    def test_clean_push_passes_without_issue(self):
        routes = {**self.compare(api_commit(C, "Feature (#5)")),
                  f"GET {R}/commits/{C}/pulls": [merged_pr()], **green_checks()}
        code, summary, out, fake = self.run_main(routes, event())
        self.assertEqual(code, 0)
        self.assertIn("passed", summary)
        self.assertIn("result=pass", out)
        self.assertEqual(self.issue_bodies(fake), [])
        self.assertEqual(self.statuses(fake)[C]["state"], "success")
        self.assertEqual(self.statuses(fake)[C]["context"], "ci-policy/main-watch")
        self.assertEqual(self.statuses(fake)[C]["description"], "PR #5: checks green")
        self.assertIn("/actions/runs/999", self.statuses(fake)[C]["target_url"])

    def test_direct_push_opens_issue_and_fails(self):
        routes = {**self.compare(api_commit(C, "hotfix straight to main")),
                  f"GET {R}/commits/{C}/pulls": [],
                  f"GET {R}/issues": [], f"POST {R}/labels": {"name": "main-watch"},
                  f"POST {R}/issues": {"number": 12}}
        code, summary, out, fake = self.run_main(routes, event())
        self.assertEqual(code, 1)
        self.assertIn("opened #12", summary)
        self.assertIn("result=alert", out)
        issue_body = self.issue_bodies(fake)[-1]
        self.assertIn("hotfix straight to main", issue_body)
        self.assertEqual(self.statuses(fake)[C]["state"], "failure")
        self.assertIn("nothing was reverted", issue_body)

    def test_fail_on_alert_false_keeps_job_green(self):
        routes = {**self.compare(api_commit(C, "hotfix")), f"GET {R}/commits/{C}/pulls": [],
                  f"GET {R}/issues": [{"number": 4}], f"POST {R}/issues/4/comments": {}}
        code, summary, _, _ = self.run_main(routes, event(), {"fail-on-alert": "false"})
        self.assertEqual(code, 0)
        self.assertIn("commented on #4", summary)

    def test_issue_failure_fails_job_even_when_not_failing_on_alert(self):
        routes = {**self.compare(api_commit(C, "hotfix")), f"GET {R}/commits/{C}/pulls": [],
                  f"GET {R}/issues": gh.GitHubError("forbidden", 403)}
        code, summary, _, _ = self.run_main(routes, event(), {"fail-on-alert": "false"})
        self.assertEqual(code, 1)
        self.assertIn("FAILED to open the issue", summary)

    def test_release_allowed_via_inputs(self):
        routes = {**self.compare(api_commit(C, "release: v1.2.3")),
                  f"GET {R}/commits/{C}": {"files": [{"filename": "VERSION"}]}}
        code, summary, _, _ = self.run_main(
            routes, event(), {"allow-rules": json.dumps(
                [{"subject": r"^release: v\d+\.\d+\.\d+$", "paths": ["VERSION"]}])})
        self.assertEqual(code, 0, summary)

    def test_release_allowed_via_allowlist_file(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "policy", "main-allowlist.json")
        fh = "/repos/drench44/family-hub"
        routes = {f"GET {fh}/compare/{A}...{C}": {"status": "ahead", "total_commits": 1,
                                                  "commits": [api_commit(C, "release: v1.4.1")]},
                  f"GET {fh}/commits/{C}": {"files": [
                      {"filename": "VERSION"}, {"filename": "CHANGELOG.md"},
                      {"filename": "src/family_hub/web/static/index.html"}]}}
        code, summary, _, _ = self.run_main(routes, event(), {"allowlist-file": path},
                                            repo="drench44/family-hub")
        self.assertEqual(code, 0, summary)
        self.assertIn("allowed by release script", summary)

    def test_other_branch_is_skipped(self):
        code, summary, _, fake = self.run_main({}, event(ref="refs/heads/feature"))
        self.assertEqual(code, 0)
        self.assertIn("skipped", summary)
        self.assertEqual(fake.calls, [])

    def test_branch_input_overrides_default(self):
        ev = event(ref="refs/heads/master")
        routes = {**self.compare(api_commit(C, "x")), f"GET {R}/commits/{C}/pulls": [
            merged_pr(base="master")], **green_checks()}
        code, summary, _, _ = self.run_main(routes, ev, {"branch": "master"})
        self.assertEqual(code, 0, summary)

    def test_api_failure_fails_loudly_and_opens_an_issue(self):
        routes = {f"GET {R}/compare/{A}...{C}": gh.GitHubError("down", 500),
                  f"GET {R}/issues": [], f"POST {R}/labels": {},
                  f"POST {R}/issues": {"number": 12}}
        code, summary, _, fake = self.run_main(routes, event())
        self.assertEqual(code, 1)
        self.assertIn("could not run", summary)
        issue = [b for p, b in fake.posts if p == f"{R}/issues"]
        self.assertEqual(len(issue), 1)
        self.assertIn("could not check a push", issue[0]["body"])

    def test_unexpected_error_is_loud_too(self):
        routes = {f"GET {R}/compare/{A}...{C}": {"status": "ahead", "total_commits": 1,
                                                  "commits": [api_commit(C, "x")]},
                  f"GET {R}/commits/{C}/pulls": lambda _: 1 / 0,
                  f"GET {R}/issues": [], f"POST {R}/labels": {},
                  f"POST {R}/issues": {"number": 3}}
        code, summary, _, _ = self.run_main(routes, event())
        self.assertEqual(code, 1)
        self.assertIn("ZeroDivisionError", summary)

    def test_push_to_other_protected_branch_is_an_error(self):
        # The caller watches main but main-watch was told another branch:
        # nothing was checked, so the run must not look green.
        with mock.patch.object(gh, "annotate") as annotate:
            code, _, _, _ = self.run_main({}, event(ref="refs/heads/master"))
        self.assertEqual(code, 1)
        self.assertEqual(annotate.call_args[0][0], "error")

    def test_rewind_marks_the_new_tip_failed(self):
        routes = {**self.compare(status="behind"),
                  f"GET {R}/issues": [], f"POST {R}/labels": {}, f"POST {R}/issues": {"number": 2}}
        code, _, _, fake = self.run_main(routes, event())
        self.assertEqual(code, 1)
        st = self.statuses(fake)[C]
        self.assertEqual(st["state"], "failure")
        self.assertIn("Force push", st["description"])

    def test_bad_allow_rules_fail_loudly(self):
        code, summary, _, _ = self.run_main({}, event(), {"allow-rules": "nope"})
        self.assertEqual(code, 1)
        self.assertIn("allow-rules", summary)

    def test_force_push_with_clean_commits_still_alerts(self):
        routes = {**self.compare(api_commit(C, "x"), status="diverged"),
                  f"GET {R}/commits/{C}/pulls": [merged_pr()], **green_checks(),
                  f"GET {R}/issues": [], f"POST {R}/labels": {}, f"POST {R}/issues": {"number": 1}}
        code, summary, _, fake = self.run_main(routes, event(forced=True))
        self.assertEqual(code, 1)
        self.assertIn("Force push", self.issue_bodies(fake)[-1])


    def test_checks_running_at_merge_wait_without_an_issue(self):
        routes = {**self.compare(api_commit(C, "Stats update (#5)")),
                  f"GET {R}/commits/{C}/pulls": [merged_pr()],
                  **green_checks(runs=[run("lint"), run("Vitest", status="queued")])}
        with mock.patch.object(main_watch, "utcnow", return_value=NOW):
            code, summary, out, fake = self.run_main(routes, event())
        self.assertEqual(code, 0, summary)
        self.assertIn("result=pending", out)
        self.assertIn("WAIT", summary)
        self.assertEqual(self.issue_bodies(fake), [])
        st = self.statuses(fake)[C]
        self.assertEqual(st["state"], "pending")
        self.assertTrue(st["description"].startswith("PR #5: waiting on head"), st)

    def test_status_write_failure_fails_the_job(self):
        routes = {**self.compare(api_commit(C, "Feature (#5)")),
                  f"GET {R}/commits/{C}/pulls": [merged_pr()], **green_checks(),
                  f"POST {R}/statuses/*": gh.GitHubError("Resource not accessible", 403)}
        with mock.patch.object(gh, "annotate") as annotate:
            code, summary, _, _ = self.run_main(routes, event())
        self.assertEqual(code, 1)
        self.assertIn("Could not set the ci-policy/main-watch status", summary)
        self.assertTrue(any("statuses: write" in c[0][1] for c in annotate.call_args_list))

    def test_set_status_false_writes_none(self):
        routes = {**self.compare(api_commit(C, "Feature (#5)")),
                  f"GET {R}/commits/{C}/pulls": [merged_pr()], **green_checks()}
        code, _, _, fake = self.run_main(routes, event(), {"set-status": "false"})
        self.assertEqual(code, 0)
        self.assertEqual(self.statuses(fake), {})

    def test_bad_timeout_input_fails_loudly(self):
        code, summary, _, _ = self.run_main({}, event(), {"pending-timeout-minutes": "-5"})
        self.assertEqual(code, 1)
        self.assertIn("pending-timeout-minutes", summary)

    def test_workflow_ref_feeds_auto_require(self):
        routes = {**self.compare(api_commit(C, "Feature (#5)")),
                  f"GET {R}/commits/{C}/pulls": [merged_pr()], **green_checks(runs=[]),
                  f"GET {R}/actions/workflows": {"workflows": [
                      {"path": ".github/workflows/main-watch.yml", "state": "active"}]}}
        code, summary, _, _ = self.run_main(
            routes, event(), extra={
                "GITHUB_WORKFLOW_REF":
                    "drench44/demo/.github/workflows/main-watch.yml@refs/heads/main"})
        self.assertEqual(code, 0, summary)
        self.assertIn("none are required", summary)


class RecheckTests(unittest.TestCase):
    """The scheduled run re-reads checks for commits a push left pending."""

    run_main = MainTests.run_main
    issue_bodies = staticmethod(MainTests.issue_bodies)
    statuses = staticmethod(MainTests.statuses)

    SCHEDULE = {"GITHUB_EVENT_NAME": "schedule"}

    def recheck(self, routes, now=NOW, inputs=None):
        ev = {"schedule": "17 * * * *", "repository": {"default_branch": "main"}}
        with mock.patch.object(main_watch, "utcnow", return_value=now):
            return self.run_main(routes, ev, inputs, extra=self.SCHEDULE)

    def base_routes(self, head_runs):
        return {
            f"GET {R}/commits": [api_commit(C, "Stats update (#5)"), api_commit(B, "older")],
            f"GET {R}/commits/{C}/status": {"statuses": [
                {"context": "ci-policy/main-watch", "state": "pending",
                 "description": "PR #5: waiting on head eeeeeee checks: Vitest is queued"}]},
            f"GET {R}/commits/{B}/status": {"statuses": [
                {"context": "ci-policy/main-watch", "state": "success"}]},
            f"GET {R}/pulls/5": merged_pr(),
            **green_checks(runs=head_runs),
        }

    def test_pending_commit_turns_green(self):
        code, summary, out, fake = self.recheck(self.base_routes([run("lint"), run("Vitest")]))
        self.assertEqual(code, 0, summary)
        self.assertIn("result=pass", out)
        self.assertEqual(set(self.statuses(fake)), {C})
        self.assertEqual(self.statuses(fake)[C]["state"], "success")
        self.assertEqual(self.issue_bodies(fake), [])
        # By position, not date: a commit inside a merged PR keeps an old date.
        params = [c for c in fake.calls if c[1] == f"{R}/commits"][0][2]
        self.assertEqual(params, {"sha": "main", "per_page": 50})

    def test_still_running_stays_pending(self):
        code, _, out, fake = self.recheck(self.base_routes([run("Vitest", status="queued")]))
        self.assertEqual(code, 0)
        self.assertIn("result=pending", out)
        self.assertEqual(self.statuses(fake)[C]["state"], "pending")

    def test_timeout_flags_and_opens_an_issue(self):
        routes = {**self.base_routes([run("Vitest", status="queued")]),
                  f"GET {R}/issues": [], f"POST {R}/labels": {},
                  f"POST {R}/issues": {"number": 21}}
        code, summary, out, fake = self.recheck(routes, now=LATE)
        self.assertEqual(code, 1)
        self.assertIn("result=alert", out)
        self.assertEqual(self.statuses(fake)[C]["state"], "failure")
        body = self.issue_bodies(fake)[-1]
        self.assertIn("re-checked commits", body)
        self.assertIn("still not green 240 minutes after the merge", body)

    def test_late_failure_flags(self):
        routes = {**self.base_routes([run("Vitest", conclusion="failure")]),
                  f"GET {R}/issues": [{"number": 3}], f"POST {R}/issues/3/comments": {}}
        code, summary, _, fake = self.recheck(routes)
        self.assertEqual(code, 1)
        self.assertIn("commented on #3", summary)

    def test_nothing_pending_is_quiet(self):
        routes = {f"GET {R}/commits": [api_commit(B, "older")],
                  f"GET {R}/commits/{B}/status": {"statuses": []}}
        code, summary, out, fake = self.recheck(routes)
        self.assertEqual(code, 0)
        self.assertIn("result=pass", out)
        self.assertIn("is waiting on checks", summary)
        self.assertIn("None of the 1 newest commits", summary)
        self.assertEqual(fake.posts, [])

    def test_description_without_pr_falls_back_to_a_full_verdict(self):
        routes = {f"GET {R}/commits": [api_commit(C, "x")],
                  f"GET {R}/commits/{C}/status": {"statuses": [
                      {"context": "ci-policy/main-watch", "state": "pending",
                       "description": "something else"}]},
                  f"GET {R}/commits/{C}/pulls": [merged_pr()], **green_checks()}
        code, summary, _, fake = self.recheck(routes)
        self.assertEqual(code, 0, summary)
        self.assertEqual(self.statuses(fake)[C]["state"], "success")

    def test_recheck_api_failure_is_loud(self):
        routes = {f"GET {R}/commits": gh.GitHubError("down", 502),
                  f"GET {R}/issues": [], f"POST {R}/labels": {},
                  f"POST {R}/issues": {"number": 4}}
        code, summary, _, fake = self.recheck(routes)
        self.assertEqual(code, 1)
        self.assertIn("could not re-check pending commits", self.issue_bodies(fake)[0])

if __name__ == "__main__":
    unittest.main()
