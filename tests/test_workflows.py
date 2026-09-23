"""Static checks on this repo's workflow files (stdlib only, no YAML parser)."""

import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")))
EXAMPLES = sorted(glob.glob(os.path.join(ROOT, "examples", "*.yml")))
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def uses_lines(path):
    for n, line in enumerate(read(path).splitlines(), 1):
        m = USES.match(line)
        if m:
            yield n, m.group(1), m.group(2).strip()


def permissions(text):
    """The top-level permissions block as {scope: level}."""
    m = re.search(r"^permissions:\n((?:  \S.*\n)+)", text, re.M)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        k, _, v = line.strip().partition(":")
        out[k.strip()] = v.split("#")[0].strip()
    return out


class PinTests(unittest.TestCase):
    def test_every_third_party_action_is_pinned_by_sha_with_a_version_comment(self):
        self.assertTrue(WORKFLOWS)
        for path in WORKFLOWS:
            for n, ref, rest in uses_lines(path):
                where = f"{os.path.basename(path)}:{n}"
                if ref.startswith("./"):
                    continue   # this repo's own reusable workflows
                with self.subTest(where=where, ref=ref):
                    if ref.startswith("docker://"):
                        self.assertRegex(ref, r"@sha256:[0-9a-f]{64}$")
                    else:
                        self.assertRegex(ref, r"@[0-9a-f]{40}$")
                    self.assertRegex(rest, r"^# v?\d+(\.\d+)*$",
                                     "needs a trailing comment naming the version")

    def test_examples_pin_ci_policy_by_sha_placeholder(self):
        for path in EXAMPLES:
            for n, ref, _ in uses_lines(path):
                with self.subTest(path=os.path.basename(path)):
                    self.assertNotRegex(ref, r"@(main|master)$")


class MainWatchWiringTests(unittest.TestCase):
    def setUp(self):
        self.text = read(os.path.join(ROOT, ".github", "workflows", "main-watch.yml"))

    def test_every_input_reaches_the_script(self):
        inputs = re.findall(r"^      ([a-z-]+):\n", self.text, re.M)
        self.assertIn("required-checks", inputs)
        for name in inputs:
            if name in ("runs-on", "ci-policy-ref"):
                continue
            env = "INPUT_" + name.upper().replace("-", "_")
            with self.subTest(input=name):
                self.assertIn(f"{env}: ${{{{ inputs.{name} }}}}", self.text)

    def test_callers_grant_what_the_reusable_workflow_asks_for(self):
        # A called workflow asking for more than its caller grants fails to start.
        wanted = permissions(self.text)
        self.assertEqual(wanted.get("statuses"), "write")
        self.assertEqual(wanted.get("actions"), "read")
        for caller in [os.path.join(ROOT, "examples", "main-watch.yml"),
                       os.path.join(ROOT, ".github", "workflows", "policy.yml")]:
            granted = permissions(read(caller))
            for scope, level in wanted.items():
                with self.subTest(caller=os.path.basename(caller), scope=scope):
                    self.assertIn(granted.get(scope), (level, "write"))

    def test_callers_schedule_the_recheck(self):
        for caller in [os.path.join(ROOT, "examples", "main-watch.yml"),
                       os.path.join(ROOT, ".github", "workflows", "policy.yml")]:
            with self.subTest(caller=os.path.basename(caller)):
                self.assertRegex(read(caller), r"schedule:\n\s+- cron: ")


class AuditWiringTests(unittest.TestCase):
    def test_every_audit_input_is_one_the_script_reads(self):
        # A typo in an INPUT_ name would quietly fall back to the default.
        text = read(os.path.join(ROOT, ".github", "workflows", "main-watch-audit.yml"))
        src = read(os.path.join(ROOT, "lib", "ci_policy", "main_watch_audit.py"))
        envs = re.findall(r"^\s+INPUT_([A-Z_]+):", text, re.M)
        self.assertIn("AUDIT_TOKEN", envs)
        for env in envs:
            name = env.lower().replace("_", "-")
            with self.subTest(input=name):
                self.assertIn(f'env_input("{name}"', src)

    def test_audit_runs_hosted_on_a_schedule_with_the_secret(self):
        text = read(os.path.join(ROOT, ".github", "workflows", "main-watch-audit.yml"))
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertRegex(text, r"schedule:\n\s+- cron: ")
        self.assertIn("secrets.MAIN_WATCH_AUDIT_TOKEN", text)
        self.assertEqual(permissions(text).get("issues"), "write")


if __name__ == "__main__":
    unittest.main()
