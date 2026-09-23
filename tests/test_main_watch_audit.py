import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

from fakes import ActionsEnv, FakeGitHub
from ci_policy import gh, main_watch_audit as audit
from ci_policy.main_watch_audit import Watched, audit_repo

NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.timezone.utc)
R = "/repos/drench44/demo"
S1, S2, S3 = "1" * 40, "2" * 40, "3" * 40
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def push(sha, hours_ago, kind="pr_merge", id_=None):
    ts = (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": id_ or int(hours_ago * 10), "after": sha, "before": "0" * 39 + "9",
            "ref": "refs/heads/main", "timestamp": ts, "activity_type": kind}


def mark(state, description=""):
    return {"statuses": [{"context": "other", "state": "success"},
                         {"context": "ci-policy/main-watch", "state": state,
                          "description": description}]}


def wf_runs(*runs):
    return {"workflow_runs": [dict({"path": ".github/workflows/main-watch.yml", "id": i,
                                    "run_attempt": 1}, **r) for i, r in enumerate(runs, 1)]}


W = Watched("drench44/demo", "main")


class AuditRepoTests(unittest.TestCase):
    def run_audit(self, routes, grace=6, lookback=72):
        return audit_repo(FakeGitHub(routes), W, NOW, grace, lookback)

    def test_concluded_pushes_are_fine(self):
        routes = {f"GET {R}/activity": [push(S1, 7), push(S2, 30)],
                  f"GET {R}/commits/{S1}/status": mark("success"),
                  f"GET {R}/commits/{S2}/status": mark("failure")}
        r = self.run_audit(routes)
        self.assertEqual(r.problems, [])
        self.assertEqual(r.checked, 2)

    def test_push_with_no_conclusion_after_grace_is_a_problem(self):
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/status": {"statuses": []},
                  f"GET {R}/actions/runs": wf_runs({"status": "queued"})}
        r = self.run_audit(routes)
        self.assertEqual([p.key for p in r.problems], [f"drench44/demo@{S1}"])
        self.assertIn("still queued", r.problems[0].text)
        self.assertIn("7.0 h after the push", r.problems[0].text)

    def test_no_run_at_all_and_cancelled_run_are_problems(self):
        for runs, text in [(wf_runs(), "no main-watch run exists"),
                           (wf_runs({"status": "completed", "conclusion": "cancelled"}),
                            "ended cancelled"),
                           (wf_runs({"status": "completed", "conclusion": "startup_failure"}),
                            "caller grant every permission"),
                           (wf_runs({"status": "completed", "conclusion": "action_required"}),
                            "ended action_required")]:
            with self.subTest(text=text):
                routes = {f"GET {R}/activity": [push(S1, 7)],
                          f"GET {R}/commits/{S1}/status": {"statuses": []},
                          f"GET {R}/actions/runs": runs}
                self.assertIn(text, self.run_audit(routes).problems[0].text)

    def test_runs_of_other_workflows_do_not_count(self):
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/status": {"statuses": []},
                  f"GET {R}/actions/runs": wf_runs({"status": "completed", "conclusion":
                                                    "success", "path": ".github/workflows/ci.yml"})}
        self.assertEqual(len(self.run_audit(routes).problems), 1)

    def test_successful_legacy_run_without_status_is_accepted(self):
        # Callers still pinned to a main-watch that set no status.
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/status": {"statuses": []},
                  f"GET {R}/actions/runs": wf_runs(
                      {"status": "completed", "conclusion": "cancelled"},
                      {"status": "completed", "conclusion": "success", "run_attempt": 2})}
        self.assertEqual(self.run_audit(routes).problems, [])

    def test_failed_run_without_status_is_not_proof(self):
        # A job killed by its timeout leaves a failed run and no status.
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/status": {"statuses": []},
                  f"GET {R}/actions/runs": wf_runs({"status": "completed",
                                                    "conclusion": "failure"})}
        (p,) = self.run_audit(routes).problems
        self.assertIn("ended failure and left no status", p.text)

    def test_pending_commit_inside_a_push_is_found(self):
        routes = {f"GET {R}/activity": [push(S1, 7)],
                  f"GET {R}/commits/{S1}/status": mark("success"),
                  f"GET {R}/compare/{'0' * 39 + '9'}...{S1}": {"commits": [
                      {"sha": S3}, {"sha": S2}, {"sha": S1}]},
                  f"GET {R}/commits/{S3}/status": mark("success"),
                  f"GET {R}/commits/{S2}/status": mark("pending", "PR #4: waiting")}
        (p,) = self.run_audit(routes).problems
        self.assertEqual(p.key, f"drench44/demo@{S2}")
        self.assertIn("part of push", p.text)

    def test_one_unreadable_push_does_not_hide_the_others(self):
        routes = {f"GET {R}/activity": [push(S1, 7), push(S2, 8)],
                  f"GET {R}/commits/{S1}/status": gh.GitHubError("No commit found", 422),
                  f"GET {R}/commits/{S2}/status": {"statuses": []},
                  f"GET {R}/actions/runs": wf_runs()}
        keys = sorted(p.key for p in self.run_audit(routes).problems)
        self.assertEqual(keys, [f"drench44/demo@{S1}:error", f"drench44/demo@{S2}"])

    def test_deletion_without_an_id_keys_on_its_time(self):
        a = push("0" * 40, 7, kind="branch_deletion")
        a.pop("id")
        (p,) = self.run_audit({f"GET {R}/activity": [a]}).problems
        self.assertEqual(p.key, f"drench44/demo@deleted@{a['timestamp']}")

    def test_pending_past_grace_means_the_recheck_is_not_running(self):
        routes = {f"GET {R}/activity": [push(S1, 8)],
                  f"GET {R}/commits/{S1}/status": mark("pending", "PR #5: waiting")}
        p = self.run_audit(routes).problems[0]
        self.assertIn("re-check is not running", p.text)
        self.assertIn("PR #5: waiting", p.text)

    def test_window_skips_fresh_and_old_pushes(self):
        routes = {f"GET {R}/activity": [push(S1, 1), push(S2, 100)]}
        r = self.run_audit(routes)
        self.assertEqual((r.checked, r.problems), (0, []))

    def test_same_tip_pushed_twice_is_checked_once(self):
        routes = {f"GET {R}/activity": [push(S1, 7, id_=1), push(S1, 9, id_=2)],
                  f"GET {R}/commits/{S1}/status": mark("success")}
        self.assertEqual(self.run_audit(routes).checked, 1)

    def test_branch_deletion_is_a_problem(self):
        routes = {f"GET {R}/activity": [push("0" * 40, 7, kind="branch_deletion", id_=77)]}
        p = self.run_audit(routes).problems[0]
        self.assertEqual(p.key, "drench44/demo@deleted@77")

    def test_unreadable_repo_is_a_problem_naming_the_permissions(self):
        routes = {f"GET {R}/activity": gh.GitHubError("Not Found", 404)}
        p = self.run_audit(routes).problems[0]
        self.assertEqual(p.key, "drench44/demo:read")
        self.assertIn("Commit statuses", p.text)

    def test_activity_query(self):
        api = FakeGitHub({f"GET {R}/activity": []})
        audit_repo(api, W, NOW, 6, 72)
        self.assertEqual(api.calls[0][2], {"ref": "refs/heads/main", "time_period": "week"})


class LoadReposTests(unittest.TestCase):
    def load(self, data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
        try:
            return audit.load_repos(f.name)
        finally:
            os.unlink(f.name)

    def test_shipped_file(self):
        repos = audit.load_repos(os.path.join(ROOT, "policy", "main-watch-repos.json"))
        by = {w.repo: w for w in repos}
        self.assertEqual(by["drench44/fleet-dashboard"].branch, "master")
        self.assertEqual(by["drench44/ci-policy"].workflow, ".github/workflows/policy.yml")
        self.assertIn("drench44/cpapclarity", by)

    def test_bad_files(self):
        for data in [{}, {"repos": {}}, {"repos": {"nope": {"branch": "main"}}},
                     {"repos": {"a/b": {}}},
                     {"repos": {"a/b": {"branch": "main", "workflow": "x.yml"}}}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)


class TokenExpiryTests(unittest.TestCase):
    def check(self, header):
        api = FakeGitHub({"GET /rate_limit": {}})
        if header is not None:
            api.headers["/rate_limit"] = {"GitHub-Authentication-Token-Expiration": header}
        return audit.token_expiry_problem(api, NOW)

    def test_expiry(self):
        self.assertIsNone(self.check(None))
        self.assertIsNone(self.check("2026-12-01 00:00:00 UTC"))
        p = self.check("2026-09-30 00:00:00 UTC")
        self.assertEqual(p.key, "token:expires:2026-09-30")
        self.assertEqual(self.check("soon").key, "token:expiry-unreadable")


HOME = "/repos/drench44/ci-policy"


class AuditMainTests(unittest.TestCase):
    def run_main(self, audit_routes, home_routes, inputs=None, token="pat"):
        repos = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"repos": {"drench44/demo": {"branch": "main"}}}, repos)
        repos.close()
        home = FakeGitHub(home_routes)
        pat = FakeGitHub({"GET /rate_limit": {}, **audit_routes})
        ins = {"repos-file": repos.name, **(inputs or {})}
        if token:
            ins["audit-token"] = token

        def client(tok, url):
            if not tok:
                raise gh.GitHubError("no GitHub token")
            return pat if tok == "pat" else home
        try:
            with ActionsEnv({}, ins, {"GITHUB_REPOSITORY": "drench44/ci-policy"}) as env, \
                    mock.patch.object(gh, "GitHub", side_effect=client), \
                    mock.patch.object(audit, "utcnow", return_value=NOW), \
                    mock.patch("builtins.print"):
                code = audit.main()
                return code, env.summary(), home
        finally:
            os.unlink(repos.name)

    STUCK = {f"GET {R}/activity": [push(S1, 7)],
             f"GET {R}/commits/{S1}/status": {"statuses": []},
             f"GET {R}/actions/runs": wf_runs()}

    def test_clean_run_posts_nothing(self):
        code, summary, home = self.run_main(
            {f"GET {R}/activity": [push(S1, 7)],
             f"GET {R}/commits/{S1}/status": mark("success")}, {})
        self.assertEqual(code, 0, summary)
        self.assertEqual(home.posts, [])
        self.assertIn("| drench44/demo | 1 | 0 |", summary)

    def test_new_problem_opens_an_issue_with_keys(self):
        code, summary, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
            f"POST {HOME}/issues": {"number": 9}})
        self.assertEqual(code, 0, summary)
        body = home.posts[-1][1]["body"]
        self.assertIn(f"<!-- main-watch-audit keys: drench44/demo@{S1} -->", body)
        self.assertIn("| NEW | drench44/demo |", body)
        self.assertIn("opened #9", summary)

    def test_already_reported_problem_stays_quiet_even_after_close(self):
        issue = {"number": 9, "state": "closed",
                 "body": f"x <!-- main-watch-audit keys: drench44/demo@{S1} -->"}
        code, summary, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [issue], f"GET {HOME}/issues/9/comments": []})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts, [])
        self.assertIn("already reported", summary)

    def test_standing_condition_alerts_again_once_its_issue_is_closed(self):
        # The token still cannot read the repo: closing the issue must not
        # silence that forever.
        issue = {"number": 9, "state": "closed",
                 "body": "x <!-- main-watch-audit keys: drench44/demo:read -->"}
        code, _, home = self.run_main({f"GET {R}/activity": gh.GitHubError("nope", 404)}, {
            f"GET {HOME}/issues": [issue], f"GET {HOME}/issues/9/comments": [],
            f"POST {HOME}/labels": {}, f"POST {HOME}/issues": {"number": 10}})
        self.assertEqual(code, 1)
        self.assertIn("drench44/demo:read", home.posts[-1][1]["body"])

    def test_standing_condition_in_the_open_issue_stays_quiet(self):
        issue = {"number": 9, "state": "open",
                 "body": "x <!-- main-watch-audit keys: drench44/demo:read -->"}
        code, summary, home = self.run_main({f"GET {R}/activity": gh.GitHubError("nope", 404)},
                                            {f"GET {HOME}/issues": [issue],
                                             f"GET {HOME}/issues/9/comments": []})
        self.assertEqual(code, 1)
        self.assertEqual(home.posts, [])

    def test_push_reported_in_an_older_closed_issue_stays_quiet(self):
        newer = {"number": 12, "state": "open", "body": "<!-- main-watch-audit keys: other -->"}
        older = {"number": 9, "state": "closed",
                 "body": f"<!-- main-watch-audit keys: drench44/demo@{S1} -->"}
        code, _, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [newer, older], f"GET {HOME}/issues/12/comments": [],
            f"GET {HOME}/issues/9/comments": []})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts, [])

    def test_new_problem_comments_on_the_open_issue(self):
        issue = {"number": 9, "state": "open", "body": "<!-- main-watch-audit keys: old -->"}
        code, _, home = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": [issue], f"GET {HOME}/issues/9/comments": [
                {"body": "<!-- main-watch-audit keys: older -->"}],
            f"POST {HOME}/issues/9/comments": {}})
        self.assertEqual(code, 0)
        self.assertEqual(home.posts[0][0], f"{HOME}/issues/9/comments")

    def test_missing_token_is_loud(self):
        code, summary, home = self.run_main({}, {
            f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
            f"POST {HOME}/issues": {"number": 1}}, token="")
        self.assertEqual(code, 1)
        self.assertIn("MAIN_WATCH_AUDIT_TOKEN secret is not set", home.posts[-1][1]["body"])

    def test_rejected_token_is_loud(self):
        code, _, home = self.run_main({"GET /rate_limit": gh.GitHubError("Bad credentials", 401)},
                                      {f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
                                       f"POST {HOME}/issues": {"number": 1}})
        self.assertEqual(code, 1)
        self.assertIn("token:rejected", home.posts[-1][1]["body"])

    def test_every_repo_unreadable_is_blind_and_fails(self):
        code, _, _ = self.run_main({f"GET {R}/activity": gh.GitHubError("nope", 404)},
                                   {f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
                                    f"POST {HOME}/issues": {"number": 1}})
        self.assertEqual(code, 1)

    def test_issue_write_failure_fails(self):
        code, summary, _ = self.run_main(self.STUCK, {
            f"GET {HOME}/issues": gh.GitHubError("forbidden", 403)})
        self.assertEqual(code, 1)
        self.assertIn("FAILED", summary)

    def test_bad_window_is_loud(self):
        code, _, _ = self.run_main({}, {f"GET {HOME}/issues": [], f"POST {HOME}/labels": {},
                                        f"POST {HOME}/issues": {"number": 1}},
                                   {"grace-hours": "80"})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
