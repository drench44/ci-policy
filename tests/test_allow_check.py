"""allow-check and the push judge against real git repos.

allow-check is what the Claude Code push guard calls: exit 0 allowed,
1 refused, 2 could not decide.
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

import fakes  # noqa: F401  (puts lib/ on sys.path)
from ci_policy import local_hooks

ZERO = "0" * 40


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": self.tmp, "GIT_AUTHOR_NAME": "t",
               "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.com"}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("CI_POLICY_ALLOWLIST", None)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.join(self.tmp, "repo")
        git(self.tmp, "init", "-q", "--bare", self.origin)
        git(self.tmp, "init", "-q", "-b", "main", self.repo)
        git(self.repo, "config", "core.hooksPath", "/dev/null")
        git(self.repo, "remote", "add", "origin", self.origin)
        git(self.repo, "config", "ci-policy.repo", "drench44/family-hub")
        self.commit("a.txt", "one", "one")
        git(self.repo, "push", "-q", "origin", "main")
        self.allowlist = os.path.join(self.tmp, "allow.json")
        with open(self.allowlist, "w") as f:
            json.dump({"repos": {"drench44/family-hub": [
                {"name": "release", "branch": "main", "subject": "^release: v",
                 "paths": ["VERSION"]}]}}, f)
        os.environ["CI_POLICY_ALLOWLIST"] = self.allowlist

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def commit(self, path, content, message):
        with open(os.path.join(self.repo, path), "w") as f:
            f.write(content + "\n")
        git(self.repo, "add", path)
        git(self.repo, "commit", "-qm", message)
        return git(self.repo, "rev-parse", "HEAD")

    def check(self, branch="main", src="HEAD", remote="origin"):
        err = io.StringIO()
        with redirect_stderr(err):
            code = local_hooks.allow_check(remote, branch, src, self.repo)
        return code, err.getvalue()


class AllowCheckTests(Repo):
    def test_allowlisted_commit(self):
        self.commit("VERSION", "1.0.1", "release: v1.0.1")
        self.assertEqual(self.check()[0], 0)

    def test_ordinary_commit_refused(self):
        self.commit("app.py", "x", "add app")
        code, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("without a PR", err)

    def test_release_subject_touching_more_refused(self):
        self.commit("VERSION", "1.0.1", "release: v1.0.1")
        self.commit("app.py", "x", "release: v1.0.2")
        self.assertEqual(self.check()[0], 1)

    def test_unresolvable_src_cannot_decide(self):
        self.assertEqual(self.check(src="nope")[0], 2)

    def test_unreachable_remote_cannot_decide(self):
        git(self.repo, "remote", "add", "gone", os.path.join(self.tmp, "missing.git"))
        self.assertEqual(self.check(remote="gone")[0], 2)

    def test_branch_on_remote_but_not_fetched_cannot_decide(self):
        other = os.path.join(self.tmp, "other")
        git(self.tmp, "clone", "-q", self.origin, other)
        git(other, "push", "-q", "origin", "main:master")
        code, err = self.check(branch="master")
        self.assertEqual(code, 2)
        self.assertIn("not fetched", err)

    def test_creating_branch_on_remote_with_branches_needs_allowlist(self):
        self.commit("app.py", "x", "add app")
        self.assertEqual(self.check(branch="master")[0], 1)

    def test_creating_branch_on_empty_remote_is_allowed(self):
        empty = os.path.join(self.tmp, "empty.git")
        git(self.tmp, "init", "-q", "--bare", empty)
        git(self.repo, "remote", "add", "empty", empty)
        self.assertEqual(self.check(remote="empty")[0], 0)

    def test_force_is_refused(self):
        self.commit("VERSION", "1.0.1", "release: v1.0.1")
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        self.commit("VERSION", "1.0.2", "release: v1.0.2")
        code, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("force push", err)

    def test_broken_allowlist_cannot_decide(self):
        with open(self.allowlist, "w") as f:
            f.write("{nope")
        self.commit("VERSION", "1.0.1", "release: v1.0.1")
        self.assertEqual(self.check()[0], 2)

    def test_cli_turns_errors_into_2(self):
        with mock.patch.object(local_hooks, "allow_check", side_effect=RuntimeError("boom")), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(local_hooks.main(["allow-check", "--remote", "origin",
                                               "--branch", "main"]), 2)


class JudgeTests(Repo):
    def test_author_rules_see_the_commit_author(self):
        with open(self.allowlist, "w") as f:
            json.dump({"repos": {"drench44/family-hub": [
                {"subject": "^bot:", "author": "bot@example.com"}]}}, f)
        base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "config", "user.email", "bot@example.com")
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_EMAIL": "bot@example.com"}):
            new = self.commit("x", "x", "bot: update")
        rules = local_hooks.load_rules("drench44/family-hub")
        ok, _ = local_hooks.judge_update(new, base, "main", rules, self.repo, "origin")
        self.assertTrue(ok)
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_EMAIL": "someone@example.com"}):
            newer = self.commit("y", "y", "bot: again")
        ok, _ = local_hooks.judge_update(newer, new, "main", rules, self.repo, "origin")
        self.assertFalse(ok)

    def test_delete_is_refused(self):
        ok, lines = local_hooks.judge_update(ZERO, git(self.repo, "rev-parse", "HEAD"), "main",
                                             [], self.repo, "origin")
        self.assertFalse(ok)
        self.assertIn("deleting", lines[0])

    def test_repo_name_prefers_a_github_url_over_the_override(self):
        git(self.repo, "remote", "set-url", "origin", "git@github.com:drench44/demo.git")
        self.assertEqual(local_hooks.repo_name("origin", "", self.repo), "drench44/demo")
        git(self.repo, "remote", "set-url", "origin", self.origin)
        self.assertEqual(local_hooks.repo_name("origin", "", self.repo), "drench44/family-hub")


if __name__ == "__main__":
    unittest.main()
