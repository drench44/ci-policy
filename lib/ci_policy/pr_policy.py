"""pr-policy: the pull request rules every drench44 repo shares.

Three rules, kept light on purpose. None of them asks for new paperwork beyond
what the PR-creation hook already has Claude write.

1. band        The body must say which review band ran: zero, one, two, or
               three review agents. Free wording is fine ("Review band: one
               agent (code-reviewer)", "All three review agents ran",
               "Review-band: 0 (docs only)").
2. regression  A PR whose title or body says it fixes a regression must add or
               change a test file. Escape hatch for the rare real exception:
               a `Regression-test: <why none>` line or the `no-regression-test`
               label.
3. size        Changed lines, not counting lockfiles and generated files. Over
               800: warning. Over 2,500: fails, unless the `size-override` label
               is on the PR or the body has a `Size-override: <reason>` line.

PRs by people other than the owners, and bot PRs, pass with a notice.

``evaluate`` is pure (no I/O) so the rules are unit tested directly; ``main``
does the GitHub plumbing.
"""

from __future__ import annotations

import base64
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from ci_policy import gh
from ci_policy.globs import GlobSet, merge

DEFAULT_OWNERS = ["drench44"]

DEFAULT_TEST_GLOBS = [
    "test_*", "*_test.*", "*.test.*", "*.spec.*", "*_spec.*", "*-test.*",
    "conftest.py", "*.bats",
    "**/test/**", "**/tests/**", "**/__tests__/**", "**/spec/**", "**/e2e/**",
]

DEFAULT_SIZE_IGNORE_GLOBS = [
    # lockfiles
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "bun.lockb", "bun.lock", "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock",
    "go.sum", "Gemfile.lock", "composer.lock", "flake.lock", "*.lock",
    # generated
    "**/generated/**", "*.generated.*", "*.gen.*", "*.min.js", "*.min.css",
    "*.map", "*_pb2.py", "*.pb.go", "dist/**", "build/**", "**/__snapshots__/**",
    "*.snap",
    # vendored and captured data
    "vendor/**", "**/vendor/**", "third_party/**", "**/third_party/**",
    "**/node_modules/**", "**/fixtures/**", "**/__fixtures__/**", "**/testdata/**",
]

DEFAULT_SIZE_WARN = 800
DEFAULT_SIZE_FAIL = 2500
DEFAULT_SIZE_LABEL = "size-override"
DEFAULT_NO_TEST_LABEL = "no-regression-test"

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# --------------------------------------------------------------- band wording

_COUNT = r"(zero|none|no|0|one|single|1|two|2|three|all\s+three|3)"
_COUNT_VALUE = {"zero": 0, "none": 0, "no": 0, "0": 0, "one": 1, "single": 1, "1": 1,
                "two": 2, "2": 2, "three": 3, "3": 3}

# Ordered: the explicit "band" forms win over a bare "N agents" mention.
_BAND_PATTERNS = [
    # "Review-band: 1", "Review band: three agents", "review band is none"
    re.compile(r"\breview[\s-]*band\b[^\n]{0,60}?\b" + _COUNT + r"\b", re.I),
    # "Band: ONE agent", "**Band: three agents**" at the start of a line
    re.compile(r"^[\s>*_#`-]*band\b[^\n]{0,60}?\b" + _COUNT + r"\b", re.I | re.M),
    # "(band 1, CI change)", "band: 3"
    re.compile(r"\bband\s*[:=]?\s*[*_`]*\s*([0-3])\b", re.I),
    # "three-agent band", "All three review agents ran", "ZERO agents per the band".
    # Only counts on a line that also talks about review or the band, so "no
    # agents overlap" in a layout fix is not read as a band statement.
    re.compile(r"\b" + _COUNT + r"[\s-]+(?:review[\s-]+)?agents?\b", re.I),
]
_REVIEW_CONTEXT = re.compile(r"review|\bband\b", re.I)


def strip_comments(text: str) -> str:
    """Drop HTML comments so PR template hints never count as answers."""
    return _HTML_COMMENT.sub("", text or "")


def find_band(body: str) -> Tuple[Optional[int], str]:
    """Return (number of review agents, the matching text), or (None, "")."""
    for index, pattern in enumerate(_BAND_PATTERNS):
        for m in pattern.finditer(body):
            if index == len(_BAND_PATTERNS) - 1:
                start = body.rfind("\n", 0, m.start()) + 1
                end = body.find("\n", m.end())
                line = body[start:end if end != -1 else len(body)]
                if not _REVIEW_CONTEXT.search(line):
                    continue
            word = re.sub(r"\s+", " ", m.group(1).lower())
            value = 3 if word == "all three" else _COUNT_VALUE[word]
            return value, m.group(0).strip(" \t*_#>`-")
    return None, ""


# --------------------------------------------------------- regression wording

# Phrases that mention regressions without claiming to fix one.
_NOT_A_CLAIM = re.compile(
    r"\bno\s+regressions?\b|\bwithout\s+(?:any\s+)?regressions?\b|\bregression[\s-]+(?:risk|free)\b"
    r"|\bregression[\s-]+(?:suite|tests?|rig)\b",
    re.I)
_TITLE_CLAIM = re.compile(r"\bregress(?:ion|ions|ed|es)?\b", re.I)
_BODY_CLAIMS = [
    re.compile(r"\bfix(?:es|ed|ing)?\b[^\n.]{0,60}?\bregress(?:ion|ions|ed)\b", re.I),
    re.compile(r"\bregressions?\s+(?:fix|from|introduced|caused)\b", re.I),
    re.compile(r"\bregressed\b", re.I),
    re.compile(r"\b(?:introduced|caused|broken|broke)\s+(?:in|by)\s+#\d+", re.I),
]
_NO_TEST_LINE = re.compile(r"^[ \t>*_\-]*regression-test[ \t*_]*:[ \t*_]*(\S.*)$", re.I | re.M)
_OVERRIDE_LINE = re.compile(r"^[ \t>*_\-]*size-override[ \t*_]*:[ \t*_]*(\S.*)$", re.I | re.M)


def regression_claim(title: str, body: str) -> Optional[str]:
    """Return the wording that claims a regression fix, or None."""
    t = _NOT_A_CLAIM.sub(" ", title or "")
    m = _TITLE_CLAIM.search(t)
    if m:
        return m.group(0)
    b = _NOT_A_CLAIM.sub(" ", body or "")
    for pattern in _BODY_CLAIMS:
        m = pattern.search(b)
        if m:
            return m.group(0)
    return None


def _line_value(pattern: re.Pattern, body: str) -> Optional[str]:
    for value in pattern.findall(body):
        value = value.strip(" \t*_")
        if value:
            return value
    return None


# ---------------------------------------------------------------------- model

@dataclass
class FileChange:
    path: str
    additions: int = 0
    deletions: int = 0
    status: str = "modified"
    previous_path: Optional[str] = None

    @property
    def changes(self) -> int:
        return self.additions + self.deletions

    def paths(self) -> List[str]:
        return [p for p in (self.path, self.previous_path) if p]


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    author: str
    author_is_bot: bool
    files: List[FileChange] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    files_complete: bool = True


@dataclass
class Config:
    owners: Sequence[str] = tuple(DEFAULT_OWNERS)
    tests: GlobSet = field(default_factory=lambda: GlobSet(DEFAULT_TEST_GLOBS))
    size_ignore: GlobSet = field(default_factory=lambda: GlobSet(DEFAULT_SIZE_IGNORE_GLOBS))
    size_warn: int = DEFAULT_SIZE_WARN
    size_fail: int = DEFAULT_SIZE_FAIL
    size_label: str = DEFAULT_SIZE_LABEL
    no_test_label: str = DEFAULT_NO_TEST_LABEL


@dataclass
class Check:
    rule: str
    outcome: str  # pass, fail, warn, skip
    summary: str
    details: List[str] = field(default_factory=list)


@dataclass
class Result:
    checks: List[Check]

    @property
    def passed(self) -> bool:
        return all(c.outcome != "fail" for c in self.checks)


def _list(paths: Sequence[str], limit: int = 20) -> List[str]:
    shown = [f"`{p}`" for p in paths[:limit]]
    if len(paths) > limit:
        shown.append(f"... and {len(paths) - limit} more")
    return shown


def _has_label(pr: PullRequest, name: str) -> bool:
    return bool(name) and name.lower() in {label.lower() for label in pr.labels}


def evaluate(pr: PullRequest, cfg: Config) -> Result:
    checks: List[Check] = []
    owners = {o.lower() for o in cfg.owners}

    if pr.author_is_bot:
        return Result([Check("author", "skip",
                             f"@{pr.author} is a bot, so the policy does not apply.")])
    if pr.author.lower() not in owners:
        return Result([Check("author", "skip",
                             f"@{pr.author} is not an owner ({', '.join(sorted(cfg.owners))}), "
                             "so the policy does not apply.")])

    body = strip_comments(pr.body)
    title = pr.title or ""

    # 1. Review band.
    band, said = find_band(body)
    if band is None:
        checks.append(Check("band", "fail", "The PR body does not say which review band ran.",
                            ["Add one line, for example `Review band: one agent "
                             "(code-reviewer), ordinary code change`. Zero agents is fine "
                             "for docs-only changes; say so."]))
    else:
        checks.append(Check("band", "pass", f"{band} review agent(s): \"{said}\"."))

    # 2. A regression fix needs a test.
    claim = regression_claim(title, body) or (
        "the `regression` label" if _has_label(pr, "regression") else None)
    if claim:
        tests = [f.path for f in pr.files
                 if f.status != "removed" and any(cfg.tests.matches(p) for p in f.paths())]
        reason = _line_value(_NO_TEST_LINE, body)
        if tests:
            checks.append(Check("regression-test", "pass",
                                f"Says \"{claim}\" and adds or changes {len(tests)} test "
                                "file(s).", _list(tests, 5)))
        elif reason or _has_label(pr, cfg.no_test_label):
            why = f"`Regression-test: {reason}`" if reason else f"the `{cfg.no_test_label}` label"
            checks.append(Check("regression-test", "warn",
                                f"Says \"{claim}\" with no test change, allowed by {why}."))
        else:
            checks.append(Check("regression-test", "fail",
                                f"Says \"{claim}\" but adds or changes no test file.",
                                ["A regression fix needs a test that fails without the fix. "
                                 "If a test truly cannot exist, add a line "
                                 "`Regression-test: <why>` to the body.",
                                 "Test files match: " + ", ".join(
                                     f"`{g}`" for g in cfg.tests.patterns) + "."]))
    else:
        checks.append(Check("regression-test", "skip",
                            "Does not claim a regression fix, so no test is required."))

    # 3. Size.
    counted = [f for f in pr.files if not any(cfg.size_ignore.matches(p) for p in f.paths())]
    ignored = [f for f in pr.files if f not in counted]
    size = sum(f.changes for f in counted)
    ignored_size = sum(f.changes for f in ignored)
    note = (f" ({ignored_size} more lines in {len(ignored)} lockfile, generated or vendored "
            "file(s) not counted)" if ignored else "")
    biggest = [f"`{f.path}` {f.changes}" for f in sorted(counted, key=lambda f: -f.changes)[:5]]
    if not pr.files_complete:
        note += "; GitHub listed only part of the changed files, so the real size is larger"
    if size > cfg.size_fail or not pr.files_complete:
        reason = _line_value(_OVERRIDE_LINE, body)
        if _has_label(pr, cfg.size_label):
            checks.append(Check("size", "warn",
                                f"{size} changed lines, over the {cfg.size_fail} limit, allowed "
                                f"by the `{cfg.size_label}` label{note}.", biggest))
        elif reason:
            checks.append(Check("size", "warn",
                                f"{size} changed lines, over the {cfg.size_fail} limit, allowed "
                                f"by `Size-override: {reason}`{note}.", biggest))
        else:
            checks.append(Check("size", "fail",
                                (f"{size} changed lines is over the {cfg.size_fail} limit{note}."
                                 if pr.files_complete else
                                 f"at least {size} changed lines{note}."),
                                [f"Split the PR, or add the `{cfg.size_label}` label, or add a "
                                 "`Size-override: <reason>` line to the body."] + biggest))
    elif size > cfg.size_warn:
        checks.append(Check("size", "warn",
                            f"{size} changed lines is over {cfg.size_warn}; consider splitting"
                            f"{note}.", biggest))
    else:
        checks.append(Check("size", "pass", f"{size} changed lines{note}."))

    return Result(checks)


_ICON = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}


def render_summary(pr: PullRequest, result: Result) -> str:
    verdict = "passed" if result.passed else "FAILED"
    lines = [f"## pr-policy {verdict} for #{pr.number}", "",
             "| Rule | Result | What it found |", "| --- | --- | --- |"]
    for c in result.checks:
        lines.append(f"| {c.rule} | {_ICON[c.outcome]} | {gh.md_escape(c.summary)} |")
    detailed = [c for c in result.checks if c.details]
    if detailed:
        lines.append("")
        for c in detailed:
            lines.append(f"**{c.rule}**")
            lines.extend(f"- {d}" for d in c.details)
            lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ plumbing

def gitattributes_globs(text: str) -> List[str]:
    """Patterns a .gitattributes marks generated or vendored (GitHub's linguist)."""
    globs: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, attrs = parts[0], parts[1:]
        for attr in attrs:
            name, _, value = attr.partition("=")
            if name in ("linguist-generated", "linguist-vendored") and value in ("", "true"):
                globs.append(pattern)
                break
    return globs


def load_config() -> Config:
    return Config(
        owners=merge(DEFAULT_OWNERS, gh.env_input("owners"), None),
        tests=GlobSet(merge(DEFAULT_TEST_GLOBS, gh.env_input("test-globs"),
                            gh.env_input("test-globs-extra"))),
        size_ignore=GlobSet(merge(DEFAULT_SIZE_IGNORE_GLOBS, gh.env_input("size-ignore-globs"),
                                  gh.env_input("size-ignore-globs-extra"))),
        size_warn=gh.env_int("size-warn", DEFAULT_SIZE_WARN),
        size_fail=gh.env_int("size-fail", DEFAULT_SIZE_FAIL),
        size_label=gh.env_input("size-override-label", DEFAULT_SIZE_LABEL),
    )


def fetch_gitattributes(api: gh.GitHub, repo: str, ref: str) -> str:
    try:
        data = api.get(f"/repos/{repo}/contents/.gitattributes", {"ref": ref})
    except gh.GitHubError as e:
        if e.status == 404:
            return ""
        raise
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return ""
    return base64.b64decode(data.get("content") or "").decode("utf-8", errors="replace")


def fetch_pull_request(api: gh.GitHub, repo: str, number: int) -> Tuple[PullRequest, str]:
    """Return the PR and its base commit sha."""
    # Read the PR fresh: a re-run replays the original event payload, which
    # would hide a body or label edited since.
    pr = api.get(f"/repos/{repo}/pulls/{number}")
    files = api.paginate(f"/repos/{repo}/pulls/{number}/files")
    changed_files = pr.get("changed_files")
    if isinstance(changed_files, int) and len(files) < changed_files:
        # The files API stops at 3000 files. The size cannot be trusted, so the
        # size rule treats the PR as over the limit (see evaluate).
        gh.annotate("warning", f"GitHub listed {len(files)} of {changed_files} changed files; "
                    "treating the PR as over the size limit.", "pr-policy")
    user = pr.get("user") or {}
    login = user.get("login") or ""
    return PullRequest(
        number=number,
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        author=login,
        author_is_bot=user.get("type") == "Bot" or login.endswith("[bot]"),
        files=[FileChange(path=f["filename"], additions=int(f.get("additions", 0)),
                          deletions=int(f.get("deletions", 0)),
                          status=f.get("status", "modified"),
                          previous_path=f.get("previous_filename")) for f in files],
        labels=[label.get("name", "") for label in pr.get("labels") or []],
        files_complete=not (isinstance(changed_files, int) and len(files) < changed_files),
    ), ((pr.get("base") or {}).get("sha") or "")


def main() -> int:
    try:
        cfg = load_config()
        event = gh.load_event()
        pr_payload = event.get("pull_request")
        if not pr_payload:
            gh.annotate("error", "pr-policy must run on a pull_request event "
                        f"(got {os.environ.get('GITHUB_EVENT_NAME', 'unknown')}).",
                        "pr-policy")
            return 1
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        api = gh.GitHub(gh.env_input("token"), os.environ.get("GITHUB_API_URL",
                                                                 "https://api.github.com"))
        pr, base_sha = fetch_pull_request(api, repo, int(pr_payload["number"]))
        extra = gitattributes_globs(fetch_gitattributes(api, repo, base_sha)) if base_sha else []
        if extra:
            cfg.size_ignore = GlobSet(cfg.size_ignore.patterns + extra)
    except (gh.GitHubError, ValueError, KeyError) as e:
        gh.annotate("error", f"pr-policy could not run: {e}", "pr-policy")
        gh.write_summary(f"## pr-policy could not run\n\n{gh.md_escape(str(e))}\n")
        return 1

    result = evaluate(pr, cfg)
    for c in result.checks:
        if c.outcome == "fail":
            gh.annotate("error", c.summary, f"pr-policy: {c.rule}")
        elif c.outcome == "warn":
            gh.annotate("warning", c.summary, f"pr-policy: {c.rule}")
        elif c.rule == "author" and c.outcome == "skip":
            gh.annotate("notice", c.summary, "pr-policy")
    gh.write_summary(render_summary(pr, result))
    gh.set_output("result", "pass" if result.passed else "fail")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
