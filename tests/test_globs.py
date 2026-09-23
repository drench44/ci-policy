import unittest

import fakes  # noqa: F401  (puts lib/ on sys.path)
from ci_policy.globs import GlobSet, merge, split_list


class GlobTests(unittest.TestCase):
    def check(self, pattern, yes=(), no=()):
        g = GlobSet([pattern])
        for p in yes:
            self.assertTrue(g.matches(p), f"{pattern!r} should match {p!r}")
        for p in no:
            self.assertFalse(g.matches(p), f"{pattern!r} should not match {p!r}")

    def test_basename_pattern_matches_at_any_depth(self):
        self.check("*.md", yes=["README.md", "docs/a/b.md"], no=["README.mdx", "md/x.txt"])

    def test_slash_pattern_is_anchored(self):
        self.check("docs/**", yes=["docs/x.md", "docs/a/b/c.png"], no=["src/docs/x.md", "docs"])

    def test_leading_double_star_matches_any_depth(self):
        self.check("**/tests/**", yes=["tests/a.py", "pkg/tests/a.py", "a/b/tests/c/d.py"],
                   no=["pkg/tests.py", "tests"])

    def test_star_does_not_cross_slash(self):
        self.check("src/*.py", yes=["src/a.py"], no=["src/sub/a.py"])

    def test_contains_pattern(self):
        self.check("*test*", yes=["src/foo.test.ts", "test_x.py", "a/pytest.ini"],
                   no=["src/tests/helper.py", "src/contest/x.py"])

    def test_prefix_pattern(self):
        self.check("CHANGELOG*", yes=["CHANGELOG.md", "pkg/CHANGELOG"], no=["OLDCHANGELOG.md"])

    def test_question_mark_and_class(self):
        self.check("v?.txt", yes=["v1.txt"], no=["v10.txt"])
        self.check("[ab].txt", yes=["a.txt", "b.txt"], no=["c.txt"])
        self.check("[!ab].txt", yes=["c.txt"], no=["a.txt"])

    def test_braces_expand(self):
        self.check("*.{yml,yaml}", yes=["a.yml", "b/c.yaml"], no=["a.json"])

    def test_trailing_slash_means_directory(self):
        self.check("vendor/", yes=["vendor/x/y.go"], no=["src/vendor/x.go"])

    def test_leading_slash_anchors(self):
        self.check("/VERSION", yes=["VERSION"], no=["pkg/VERSION"])

    def test_dots_are_literal(self):
        self.check("*.min.js", yes=["a.min.js"], no=["aminxjs", "a.min.jsx"])

    def test_empty_globset_matches_nothing(self):
        self.assertFalse(GlobSet([]).matches("a"))
        self.assertFalse(bool(GlobSet([])))


class ListTests(unittest.TestCase):
    def test_split_newlines_commas_comments(self):
        raw = "a, b\n# comment\n\n  c  \n*.{yml,yaml}"
        self.assertEqual(split_list(raw), ["a", "b", "c", "*.{yml,yaml}"])

    def test_split_empty(self):
        self.assertEqual(split_list(""), [])
        self.assertEqual(split_list(None), [])

    def test_merge_replace_and_extra(self):
        self.assertEqual(merge(["x", "y"], "", ""), ["x", "y"])
        self.assertEqual(merge(["x", "y"], "z", ""), ["z"])
        self.assertEqual(merge(["x", "y"], "", "y\nw"), ["x", "y", "w"])
        self.assertEqual(merge(["x"], "  ", "w"), ["x", "w"])


if __name__ == "__main__":
    unittest.main()
