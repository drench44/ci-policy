"""The shared allowlist of commits that may reach a protected branch without a PR.

One JSON file (policy/main-allowlist.json) feeds three guards: main-watch in
CI, the global git pre-push hook, and the Claude Code push guard. Keeping one
file means an automation is allowed everywhere or nowhere.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Pattern, Sequence

from ci_policy.globs import GlobSet

PROTECTED_BRANCHES = ("main", "master")


@dataclass
class Rule:
    name: str
    subject: Pattern[str]
    branch: Optional[str] = None
    author: Optional[str] = None
    paths: Optional[GlobSet] = None

    def describe(self) -> str:
        bits = [f"subject /{self.subject.pattern}/"]
        if self.branch:
            bits.append(f"branch {self.branch}")
        if self.author:
            bits.append(f"author {self.author}")
        if self.paths:
            bits.append("paths " + ", ".join(self.paths.patterns))
        return f"{self.name} ({'; '.join(bits)})"


def make_rule(item: Dict[str, Any], where: str) -> Rule:
    if not isinstance(item, dict) or not isinstance(item.get("subject"), str):
        raise ValueError(f"{where} needs a string \"subject\"")
    try:
        subject = re.compile(item["subject"])
    except re.error as e:
        raise ValueError(f"{where}: bad subject regex {item['subject']!r}: {e}") from None
    paths = item.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError(f"{where}: \"paths\" must be a list of globs")
    for key in ("branch", "author", "name"):
        if item.get(key) is not None and not isinstance(item[key], str):
            raise ValueError(f"{where}: \"{key}\" must be a string")
    return Rule(name=item.get("name") or where, subject=subject,
                branch=item.get("branch") or None, author=item.get("author") or None,
                paths=GlobSet(paths) if paths else None)


def parse(data: Any) -> Dict[str, List[Rule]]:
    """Validate a loaded allowlist document; return repo (lowercase) -> rules."""
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        raise ValueError("allowlist must be an object with a \"repos\" object")
    out: Dict[str, List[Rule]] = {}
    for repo, items in data["repos"].items():
        if not isinstance(items, list):
            raise ValueError(f"allowlist repo {repo} must map to a list of rules")
        out[repo.lower()] = [make_rule(item, f"{repo}[{i}]") for i, item in enumerate(items)]
    return out


def load(path: str) -> Dict[str, List[Rule]]:
    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except ValueError as e:
            raise ValueError(f"{path} is not valid JSON: {e}") from None
    return parse(data)


def rules_for(allow: Dict[str, List[Rule]], repo: Optional[str]) -> List[Rule]:
    return list(allow.get((repo or "").lower(), []))


_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)


def repo_from_url(url: str) -> Optional[str]:
    """owner/name from a GitHub remote URL (https, ssh, or scp-like), else None."""
    m = _REMOTE.search((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def match(rules: Sequence[Rule], subject: str, branch: Optional[str],
          files: Callable[[], List[str]], authors: Sequence[str] = ()) -> Optional[Rule]:
    """First rule this commit satisfies, or None.

    ``files`` is called lazily (only when a rule limits paths) and must return
    every path the commit touches; an empty list never satisfies a paths rule.
    """
    for rule in rules:
        if not rule.subject.search(subject or ""):
            continue
        if rule.branch and branch and rule.branch != branch:
            continue
        if rule.author and rule.author.lower() not in {a.lower() for a in authors if a}:
            continue
        if rule.paths:
            touched = files()
            if not touched or not all(rule.paths.matches(p) for p in touched):
                continue
        return rule
    return None
