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
        routes = {f"GET {R}/commits/{B}/pulls": [merged_pr(base="develop")], **green_checks()}
        self.assertFalse(self.watcher(routes).verdict(commit()).ok)

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
        self.assertTrue(self.watcher(routes).verdict(commit()).ok)

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
    def run_main(self, routes, ev, inputs=None, repo="drench44/demo"):
        fake = FakeGitHub(routes)
        with ActionsEnv(ev, inputs, {"GITHUB_REPOSITORY": repo}) as env, mock.patch.object(gh, "GitHub", return_value=fake), \
                mock.patch("builtins.print"):
            code = main_watch.main()
            return code, env.summary(), env.outputs(), fake

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
        self.assertEqual(fake.posts, [])

    def test_direct_push_opens_issue_and_fails(self):
        routes = {**self.compare(api_commit(C, "hotfix straight to main")),
                  f"GET {R}/commits/{C}/pulls": [],
                  f"GET {R}/issues": [], f"POST {R}/labels": {"name": "main-watch"},
                  f"POST {R}/issues": {"number": 12}}
        code, summary, out, fake = self.run_main(routes, event())
        self.assertEqual(code, 1)
        self.assertIn("opened #12", summary)
        self.assertIn("result=alert", out)
        issue_body = fake.posts[-1][1]["body"]
        self.assertIn("hotfix straight to main", issue_body)
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

    def test_api_failure_fails_loudly(self):
        routes = {f"GET {R}/compare/{A}...{C}": gh.GitHubError("down", 500)}
        code, summary, _, _ = self.run_main(routes, event())
        self.assertEqual(code, 1)
        self.assertIn("could not run", summary)

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
        self.assertIn("Force push", fake.posts[-1][1]["body"])


if __name__ == "__main__":
    unittest.main()
