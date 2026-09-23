import base64
import unittest
from unittest import mock

from fakes import ActionsEnv, FakeGitHub
from ci_policy import gh, pr_policy
from ci_policy.pr_policy import (Config, FileChange, PullRequest, evaluate, find_band,
                                 gitattributes_globs, regression_claim)

BAND1 = "Review band: one agent (code-reviewer), ordinary code change.\n"


def pr(body=BAND1, title="Add a thing", files=None, author="drench44", bot=False, labels=None):
    return PullRequest(number=7, title=title, body=body, author=author, author_is_bot=bot,
                       files=files if files is not None else [FileChange("src/app.py", 10, 2)],
                       labels=labels or [])


def check(result, rule):
    return next(c for c in result.checks if c.rule == rule)


def outcome(result, rule):
    return check(result, rule).outcome


class AuthorTests(unittest.TestCase):
    def test_bot_passes_with_skip(self):
        r = evaluate(pr(body="", author="dependabot[bot]", bot=True), Config())
        self.assertTrue(r.passed)
        self.assertEqual([c.rule for c in r.checks], ["author"])
        self.assertIn("bot", r.checks[0].summary)

    def test_non_owner_passes_with_skip(self):
        r = evaluate(pr(body="", author="somecontrib"), Config())
        self.assertTrue(r.passed)
        self.assertIn("not an owner", r.checks[0].summary)

    def test_owner_match_ignores_case(self):
        r = evaluate(pr(body="", author="Drench44"), Config())
        self.assertFalse(r.passed)

    def test_custom_owners(self):
        r = evaluate(pr(body="", author="somecontrib"), Config(owners=["drench44", "somecontrib"]))
        self.assertFalse(r.passed)


class BandWordingTests(unittest.TestCase):
    """Phrasings copied from real drench44 PR bodies (2026-08 and 2026-09)."""

    REAL = [
        ("Review-band: 1 (scripts and tooling; code-reviewer, plus silent-failure-hunter)", 1),
        ("## Review band\n\nTHREE-agent band (the diff touches `deploy/garage/ci.sh`)", 3),
        ("Band: ONE agent for ordinary code, raised by triggers", 1),
        ("Docs-only (Tier C, ZERO agents per the band: `.claude/**` only).", 0),
        ("Release-only change: VERSION, CHANGELOG. Review band: zero agents (no code change).", 0),
        ("## Review band\n\nThis is a high-risk change. All three review agents ran three times.", 3),
        ("- All three review agents ran. Their findings are fixed in the last commit.", 3),
        ("- Reviewed by the three review agents. Their findings are fixed in this branch.", 3),
        ("Three-agent review (code-reviewer, silent-failure-hunter, pr-test-analyzer), then more.", 3),
        ("Review band: three agents per this repo's CLAUDE.md.", 3),
        ("Review band: none beyond self-review. Test-only diff (4 files, fixtures only).", 0),
        ("- [x] Reviewed by the code-reviewer agent (band 1, CI/infra config change)", 1),
        ("Review: three-agent band (touches a boot unit on hardware).", 3),
        ("Review band: **zero agents**. One data row in a register, no logic.", 0),
        ("Review band: ONE agent (code-reviewer). It's a normal code change.", 1),
        ("- Review band: THREE agents (code-reviewer, silent-failure-hunter, pr-test-analyzer).", 3),
        ("**Review band: 3 agents (user-facing wall display plus the radar data path).**", 3),
        ("The band is one agent (code-reviewer), for ordinary code.", 1),
        ("Band: two agents on the board fix (code-reviewer and silent-failure-hunter)", 2),
        ("Band: **three agents** (code-reviewer, silent-failure-hunter, pr-test-analyzer)", 3),
        ("> **Review-band:** 0 (docs only)", 0),
    ]

    def test_real_phrasings_are_found(self):
        for text, want in self.REAL:
            with self.subTest(text=text):
                band, said = find_band(text)
                self.assertEqual(band, want, said)
                self.assertTrue(said)

    def test_no_band_statement(self):
        for text in ["", "Adds a thing.\n\n## Test plan\n- ran it",
                     "Holidays price with the weekend bands everywhere. Default none.",
                     "Band names and 5-9pm were hardcoded; now derived from tou.bands.",
                     "The price-band label went stale."]:
            with self.subTest(text=text):
                self.assertEqual(find_band(text), (None, ""))

    def test_band_line_wins_over_later_agent_mention(self):
        band, _ = find_band("Review band: one agent.\n\nLater three agents looked at docs.")
        self.assertEqual(band, 1)

    def test_band_in_html_comment_is_ignored(self):
        r = evaluate(pr(body="<!-- Review band: one agent -->\nNothing else."), Config())
        self.assertEqual(outcome(r, "band"), "fail")

    def test_missing_band_fails_with_example(self):
        r = evaluate(pr(body="Does a thing."), Config())
        self.assertFalse(r.passed)
        self.assertIn("Review band: one agent", check(r, "band").details[0])

    def test_present_band_passes(self):
        r = evaluate(pr(), Config())
        self.assertEqual(outcome(r, "band"), "pass")
        self.assertIn("1 review agent", check(r, "band").summary)


class RegressionWordingTests(unittest.TestCase):
    def test_claims(self):
        for title, body in [("Fix regression in leak chart", ""),
                            ("Leak chart regressed", ""),
                            ("Leak chart", "Fixes a regression from #88."),
                            ("Leak chart", "This fixes the chart regression Jon saw."),
                            ("Leak chart", "The regression introduced by the parser change."),
                            ("Leak chart", "Broken in #1089, restored here."),
                            ("Leak chart", "It regressed after the refactor.")]:
            with self.subTest(title=title, body=body):
                self.assertIsNotNone(regression_claim(title, body))

    def test_not_claims(self):
        for title, body in [("Add a thing", ""),
                            ("Add regression tests for the parser", ""),
                            ("Leak chart", "No regressions in the full suite."),
                            ("Leak chart", "Regression risk: low."),
                            ("Leak chart", "Ran the regression suite and the regression rig."),
                            ("Leak chart", "Refactor without any regression."),
                            ("Leak chart", "See #88 for context."),
                            ("Progressive loading", "Uses progression bars."),
                            ("Leak chart", "Fixed the typo. A regression suite ran.")]:
            with self.subTest(title=title, body=body):
                self.assertIsNone(regression_claim(title, body))


class RegressionRuleTests(unittest.TestCase):
    def test_claim_without_test_fails(self):
        r = evaluate(pr(title="Fix regression in the leak chart"), Config())
        self.assertEqual(outcome(r, "regression-test"), "fail")
        self.assertFalse(r.passed)

    def test_claim_with_test_passes(self):
        for path in ["tests/test_leak.py", "src/leak.test.ts", "web/lib/jobs.test.ts",
                     "src/__tests__/leak.tsx", "e2e/data.spec.ts", "test_thing.py",
                     "pkg/leak_test.go", "tests/deploy/run-tests.sh", "t/hook.bats"]:
            with self.subTest(path=path):
                files = [FileChange("src/leak.ts", 5, 1), FileChange(path, 20, 0, "added")]
                r = evaluate(pr(title="Fix regression in the leak chart", files=files), Config())
                self.assertEqual(outcome(r, "regression-test"), "pass")

    def test_non_test_lookalikes_do_not_count(self):
        for path in ["src/latest.json", "docs/attestation.md", "src/contest.ts"]:
            with self.subTest(path=path):
                files = [FileChange("src/leak.ts", 5, 1), FileChange(path, 2, 0)]
                r = evaluate(pr(title="Fix regression", files=files), Config())
                self.assertEqual(outcome(r, "regression-test"), "fail")

    def test_deleting_a_test_does_not_count(self):
        files = [FileChange("src/leak.ts", 5, 1), FileChange("tests/test_leak.py", 0, 30, "removed")]
        r = evaluate(pr(title="Fix regression", files=files), Config())
        self.assertEqual(outcome(r, "regression-test"), "fail")

    def test_renamed_into_tests_counts(self):
        files = [FileChange("tests/test_new.py", 3, 0, "renamed", previous_path="old.py")]
        r = evaluate(pr(title="Fix regression", files=files), Config())
        self.assertEqual(outcome(r, "regression-test"), "pass")

    def test_regression_label_triggers_rule(self):
        r = evaluate(pr(labels=["regression"]), Config())
        self.assertEqual(outcome(r, "regression-test"), "fail")

    def test_escape_line_turns_fail_into_warn(self):
        body = BAND1 + "Regression-test: the bug was a typo in a config value.\n"
        r = evaluate(pr(title="Fix regression", body=body), Config())
        self.assertEqual(outcome(r, "regression-test"), "warn")
        self.assertTrue(r.passed)

    def test_empty_escape_line_does_not_count(self):
        r = evaluate(pr(title="Fix regression", body=BAND1 + "Regression-test:\n"), Config())
        self.assertEqual(outcome(r, "regression-test"), "fail")

    def test_escape_label(self):
        r = evaluate(pr(title="Fix regression", labels=["no-regression-test"]), Config())
        self.assertEqual(outcome(r, "regression-test"), "warn")

    def test_no_claim_skips(self):
        r = evaluate(pr(), Config())
        self.assertEqual(outcome(r, "regression-test"), "skip")


class SizeTests(unittest.TestCase):
    def sized(self, *changes, body=BAND1, labels=None, cfg=None):
        files = [FileChange(p, a, d) for p, a, d in changes]
        return evaluate(pr(files=files, body=body, labels=labels), cfg or Config())

    def test_small_passes(self):
        self.assertEqual(outcome(self.sized(("a.py", 100, 50)), "size"), "pass")

    def test_limits_are_exclusive(self):
        self.assertEqual(outcome(self.sized(("a.py", 800, 0)), "size"), "pass")
        self.assertEqual(outcome(self.sized(("a.py", 801, 0)), "size"), "warn")
        self.assertEqual(outcome(self.sized(("a.py", 2500, 0)), "size"), "warn")
        self.assertEqual(outcome(self.sized(("a.py", 2501, 0)), "size"), "fail")

    def test_deletions_count(self):
        self.assertEqual(outcome(self.sized(("a.py", 1300, 1300)), "size"), "fail")

    def test_label_override(self):
        r = self.sized(("a.py", 3000, 0), labels=["Size-Override"])
        self.assertEqual(outcome(r, "size"), "warn")
        self.assertIn("label", check(r, "size").summary)
        self.assertTrue(r.passed)

    def test_body_line_override(self):
        r = self.sized(("a.py", 3000, 0), body=BAND1 + "Size-override: one generated migration\n")
        self.assertEqual(outcome(r, "size"), "warn")
        self.assertIn("one generated migration", check(r, "size").summary)

    def test_empty_override_line_does_not_count(self):
        r = self.sized(("a.py", 3000, 0), body=BAND1 + "Size-override:\n")
        self.assertEqual(outcome(r, "size"), "fail")

    def test_override_in_comment_does_not_count(self):
        r = self.sized(("a.py", 3000, 0), body=BAND1 + "<!-- Size-override: x -->\n")
        self.assertEqual(outcome(r, "size"), "fail")

    def test_lockfiles_and_generated_not_counted(self):
        r = self.sized(("src/a.ts", 100, 0), ("package-lock.json", 9000, 4000),
                       ("web/pnpm-lock.yaml", 5000, 0), ("uv.lock", 3000, 0),
                       ("src/api/generated/client.ts", 4000, 0), ("app.min.js", 3000, 0),
                       ("src/__snapshots__/x.snap", 3000, 0), ("tests/fixtures/night.json", 8000, 0))
        self.assertEqual(outcome(r, "size"), "pass")
        self.assertIn("100 changed lines", check(r, "size").summary)
        self.assertIn("not counted", check(r, "size").summary)

    def test_rename_out_of_ignored_path_counts(self):
        r = evaluate(pr(files=[FileChange("src/big.ts", 3000, 0, "renamed",
                                          previous_path="vendor/big.ts")]), Config())
        # Either path being ignored is enough to skip it: moving vendored code is not authored.
        self.assertEqual(outcome(r, "size"), "pass")

    def test_custom_limits(self):
        r = self.sized(("a.py", 60, 0), cfg=Config(size_warn=10, size_fail=50))
        self.assertEqual(outcome(r, "size"), "fail")

    def test_biggest_files_listed(self):
        r = self.sized(("a.py", 2000, 0), ("b.py", 900, 0))
        self.assertIn("`a.py` 2000", check(r, "size").details)


class GitattributesTests(unittest.TestCase):
    def test_generated_and_vendored(self):
        text = ("# comment\n*.png binary\nsrc/gen/** linguist-generated\n"
                "board/*.html linguist-vendored=true\nkeep.ts linguist-generated=false\n"
                "api.ts -diff linguist-generated=true\n")
        self.assertEqual(gitattributes_globs(text), ["src/gen/**", "board/*.html", "api.ts"])

    def test_empty(self):
        self.assertEqual(gitattributes_globs(""), [])


class SummaryTests(unittest.TestCase):
    def test_summary_explains_every_rule(self):
        r = evaluate(pr(title="Fix regression", body="nothing"), Config())
        text = pr_policy.render_summary(pr(), r)
        self.assertIn("pr-policy FAILED for #7", text)
        for rule in ("band", "regression-test", "size"):
            self.assertIn(f"| {rule} |", text)

    def test_pipes_escaped(self):
        r = evaluate(pr(body="Review band | one agent"), Config())
        self.assertIn("\\|", pr_policy.render_summary(pr(), r))


def gh_routes(body=BAND1, files=None, login="drench44", user_type="User", changed_files=None,
              labels=None, gitattributes=None):
    files = files if files is not None else [{"filename": "a.py", "additions": 4,
                                              "deletions": 2}]
    pull = {"number": 7, "title": "Add a thing", "body": body,
            "user": {"login": login, "type": user_type},
            "labels": [{"name": n} for n in labels or []],
            "base": {"sha": "b" * 40}}
    if changed_files is not None:
        pull["changed_files"] = changed_files
    routes = {"GET /repos/drench44/demo/pulls/7": pull,
              "GET /repos/drench44/demo/pulls/7/files": files}
    if gitattributes is not None:
        routes["GET /repos/drench44/demo/contents/.gitattributes"] = {
            "encoding": "base64", "content": base64.b64encode(gitattributes.encode()).decode()}
    return routes


class MainTests(unittest.TestCase):
    def run_main(self, routes, event=None, inputs=None):
        event = event if event is not None else {"pull_request": {"number": 7, "body": "stale"}}
        fake = FakeGitHub(routes)
        with ActionsEnv(event, inputs) as env, \
                mock.patch.object(gh, "GitHub", return_value=fake), \
                mock.patch("builtins.print"):
            code = pr_policy.main()
            return code, env.summary(), env.outputs()

    def test_passes_end_to_end(self):
        code, summary, out = self.run_main(gh_routes())
        self.assertEqual(code, 0)
        self.assertIn("pr-policy passed", summary)
        self.assertIn("result=pass", out)

    def test_reads_body_fresh_not_from_event(self):
        # The event body says "stale"; the API body has the band, so it passes.
        code, _, _ = self.run_main(gh_routes(body=BAND1))
        self.assertEqual(code, 0)

    def test_fails_end_to_end(self):
        code, summary, out = self.run_main(gh_routes(body="no band here"))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", summary)
        self.assertIn("result=fail", out)

    def test_bot_user_type(self):
        code, summary, _ = self.run_main(gh_routes(body="", login="renovate", user_type="Bot"))
        self.assertEqual(code, 0)
        self.assertIn("bot", summary)

    def test_not_a_pull_request_event(self):
        code, _, _ = self.run_main(gh_routes(), event={"ref": "refs/heads/main"})
        self.assertEqual(code, 1)

    def test_api_error_fails_closed(self):
        routes = gh_routes()
        routes["GET /repos/drench44/demo/pulls/7/files"] = gh.GitHubError("boom", 500)
        code, summary, _ = self.run_main(routes)
        self.assertEqual(code, 1)
        self.assertIn("could not run", summary)

    def test_gitattributes_server_error_fails_closed(self):
        routes = gh_routes()
        routes["GET /repos/drench44/demo/contents/.gitattributes"] = gh.GitHubError("x", 502)
        code, _, _ = self.run_main(routes)
        self.assertEqual(code, 1)

    def test_bad_input_fails_closed(self):
        code, summary, _ = self.run_main(gh_routes(), inputs={"size-warn": "lots"})
        self.assertEqual(code, 1)
        self.assertIn("size-warn", summary)

    def test_inputs_reach_config(self):
        code, _, _ = self.run_main(gh_routes(), inputs={"size-warn": "1", "size-fail": "3"})
        self.assertEqual(code, 1)  # 6 changed lines > 3

    def test_label_from_api_overrides_size(self):
        code, _, _ = self.run_main(gh_routes(labels=["big-ok"]),
                                   inputs={"size-fail": "3", "size-override-label": "big-ok"})
        self.assertEqual(code, 0)

    def test_gitattributes_generated_not_counted(self):
        files = [{"filename": "src/gen/api.ts", "additions": 5000, "deletions": 0}]
        code, _, _ = self.run_main(gh_routes(files=files))
        self.assertEqual(code, 1)
        code, summary, _ = self.run_main(gh_routes(files=files,
                                                   gitattributes="src/gen/** linguist-generated\n"))
        self.assertEqual(code, 0, summary)

    def test_extra_ignore_glob_input(self):
        files = [{"filename": "board/index.html", "additions": 5000, "deletions": 0}]
        code, _, _ = self.run_main(gh_routes(files=files),
                                   inputs={"size-ignore-globs-extra": "board/**"})
        self.assertEqual(code, 0)

    def test_owners_input(self):
        code, _, _ = self.run_main(gh_routes(body="", login="somecontrib"),
                                   inputs={"owners": "drench44, somecontrib"})
        self.assertEqual(code, 1)

    def test_truncated_file_list_warns(self):
        with mock.patch.object(gh, "annotate") as annotate:
            self.run_main(gh_routes(changed_files=5000))
        self.assertTrue(any("5000" in str(c) for c in annotate.call_args_list))


if __name__ == "__main__":
    unittest.main()
