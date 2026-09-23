"""Unit tests for the pre-commit scan and push guard helpers.

Fake secrets are assembled at runtime so this file never holds a string the
scanner (or any other scanner) would flag.
"""

import unittest

import fakes  # noqa: F401  (puts lib/ on sys.path)
from ci_policy import local_hooks
from ci_policy.globs import GlobSet
from ci_policy.local_hooks import StagedFile, parse_diff, protected, scan

DASH = chr(0x2014)
NONE = GlobSet([])


def staged(path, *lines, binary=False):
    return StagedFile(path, binary, [(i + 1, t) for i, t in enumerate(lines)])


class ParseDiffTests(unittest.TestCase):
    DIFF = "\n".join([
        "diff --git a/a.txt b/a.txt",
        "index 1..2 100644",
        "--- a/a.txt",
        "+++ b/a.txt",
        "@@ -3,0 +4,2 @@ context",
        "+new line",
        "++++ looks like a header but is content",
        "@@ -10 +12 @@",
        "-old",
        "+replaced",
        "diff --git a/new.py b/new.py",
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/new.py",
        "@@ -0,0 +1 @@",
        "+x = 1",
        "\\ No newline at end of file",
        "diff --git a/img.png b/img.png",
        "Binary files /dev/null and b/img.png differ",
        'diff --git "a/sp\\"q.txt" "b/sp\\"q.txt"',
        '+++ "b/sp\\"q.txt"',
        "@@ -0,0 +1 @@",
        "+quoted",
    ])

    def test_parse(self):
        files = {f.path: f for f in parse_diff(self.DIFF)}
        self.assertEqual(files["a.txt"].added,
                         [(4, "new line"), (5, "+++ looks like a header but is content"),
                          (12, "replaced")])
        self.assertEqual(files["new.py"].added, [(1, "x = 1")])
        self.assertTrue(files["img.png"].binary)
        self.assertEqual(files['sp"q.txt'].added, [(1, "quoted")])

    def test_empty(self):
        self.assertEqual(parse_diff(""), [])

    def test_space_in_path_drops_gits_trailing_tab(self):
        diff = "diff --git a/my notes.md b/my notes.md\n+++ b/my notes.md\t\n@@ -0,0 +1 @@\n+x"
        self.assertEqual([f.path for f in parse_diff(diff)], ["my notes.md"])


class ScanTests(unittest.TestCase):
    def kinds(self, files, emdash=NONE, secret=NONE, names=()):
        return [(f.kind, f.path) for f in scan(files, emdash, secret, names)]

    def test_em_dash(self):
        found = scan([staged("a.md", "fine", f"a {DASH} b")], NONE, NONE)
        self.assertEqual([(f.kind, f.line) for f in found], [("em dash", 2)])

    def test_en_dash_and_escape_are_fine(self):
        self.assertEqual(self.kinds([staged("a.ts", "1–2", "'\\u2014'")]), [])

    def test_em_dash_allow_glob(self):
        self.assertEqual(self.kinds([staged("vendor/x.html", DASH)],
                                    emdash=GlobSet(["vendor/**"])), [])

    def test_binary_skipped(self):
        self.assertEqual(self.kinds([staged("a.png", binary=True)]), [])

    def test_text_marked_binary_gets_the_secret_scan(self):
        secret = "gh" + "p_" + "C" * 36
        found = scan([staged("conf.yml", binary=True)], NONE, NONE,
                     blob_text=lambda path: f"a: 1\ntoken: {secret}\n")
        self.assertEqual([(f.kind, f.line) for f in found], [("GitHub token", 2)])
        found = scan([staged("img.png", binary=True)], NONE, NONE, blob_text=lambda path: None)
        self.assertEqual(found, [])

    def test_secret_patterns(self):
        samples = {
            "GitHub token": "gh" + "p_" + "A1b2" * 9,
            "GitHub fine-grained token": "github" + "_pat_" + "a1B2" * 20,
            "AWS access key id": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
            "Anthropic API key": "sk-" + "ant-" + "api03-" + "x" * 30,
            "Slack token": "xo" + "xb-" + "123456789012-abcdefghij",
            "Stripe live key": "sk" + "_live_" + "a" * 24,
            "Google API key": "AI" + "za" + "B" * 35,
            "private key": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
            "JSON Web Token": "ey" + "J" + "a" * 20 + ".ey" + "J" + "b" * 20 + "." + "c" * 20,
            "npm token": "np" + "m_" + "Z" * 36,
            "Supabase access or secret key": "sb" + "p_" + "a" * 40,
        }
        for kind, secret in samples.items():
            with self.subTest(kind=kind):
                found = scan([staged("c.py", f"KEY = '{secret}'")], NONE, NONE)
                self.assertEqual([f.kind for f in found], [kind])
                self.assertNotIn(secret, found[0].render())

    def test_near_misses_are_not_secrets(self):
        for text in ["ghp_short", "sk-ant-", "AKIA123", "eyJhbGciOi", "sk_test_" + "a" * 30,
                     "password = os.environ['PW']", "token: ${{ github.token }}",
                     "https://hooks.slack.com/services/", "npm_config_cache=/tmp"]:
            with self.subTest(text=text):
                self.assertEqual(self.kinds([staged("c.py", text)]), [])

    def test_allow_marker_and_glob(self):
        secret = "gh" + "p_" + "A" * 36
        self.assertEqual(self.kinds([staged("c.py", f"'{secret}'  # ci-policy: allow-secret")]),
                         [])
        self.assertEqual(self.kinds([staged("tests/fx.py", secret)],
                                    secret=GlobSet(["tests/**"])), [])

    def test_secret_file_names(self):
        for path in [".env", "deploy/.env", ".env.local", ".env.production", "id_rsa",
                     "keys/id_ed25519", "cert.p12", "a/b.keystore", ".netrc",
                     "service-account-prod.json", "credentials.json"]:
            with self.subTest(path=path):
                self.assertEqual(self.kinds([], names=[path]), [("secret file", path)])

    def test_example_file_names_are_fine(self):
        for path in [".env.example", ".env.sample", ".env.template", "id_rsa.pub",
                     "src/environment.ts", "docs/env.md", "a.envrc"]:
            with self.subTest(path=path):
                self.assertEqual(self.kinds([], names=[path]), [])

    def test_name_listed_once_even_when_also_in_diff(self):
        found = self.kinds([staged(".env", "A=1")], names=[".env"])
        self.assertEqual(found, [("secret file", ".env")])


class ProtectedTests(unittest.TestCase):
    def test_protected(self):
        self.assertEqual(protected("refs/heads/main"), "main")
        self.assertEqual(protected("refs/heads/master"), "master")
        self.assertIsNone(protected("refs/heads/main-fix"))
        self.assertIsNone(protected("refs/tags/main"))
        self.assertIsNone(protected("refs/heads/feature/main"))


class LoadRulesTests(unittest.TestCase):
    def test_missing_allowlist_raises(self):
        with self.assertRaises(OSError):
            local_hooks.load_rules("drench44/x", "/nonexistent/allowlist.json")

    def test_shipped_allowlist_loads(self):
        self.assertTrue(local_hooks.load_rules("drench44/family-hub"))


class CliTests(unittest.TestCase):
    def test_unknown_command_is_usage_error(self):
        with self.assertRaises(SystemExit):
            local_hooks.main(["nope"])


if __name__ == "__main__":
    unittest.main()
