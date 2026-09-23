"""Path glob matching shared by pr-policy and main-watch.

Rules (close to .gitignore, and the same for every input that takes globs):

* A pattern with no slash matches the file's base name at any depth:
  ``*.md`` matches ``README.md`` and ``docs/a/b.md``.
* A pattern with a slash is anchored at the repo root: ``docs/**`` matches
  ``docs/x.md`` but not ``src/docs/x.md``. Start it with ``**/`` to match at
  any depth: ``**/tests/**``.
* ``*`` matches within one path segment, ``**`` matches across segments,
  ``?`` matches one character, ``[abc]`` is a character class, and
  ``{a,b}`` expands to alternatives (not nested).
* A trailing slash means "everything under this directory":
  ``vendor/`` is the same as ``vendor/**``.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Pattern, Sequence


def split_list(raw: str | None) -> List[str]:
    """Split a multi-line or comma-separated action input into items.

    Blank items and ``#`` comment lines are dropped.
    """
    if not raw:
        return []
    items: List[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in _split_commas(line):
            part = part.strip()
            if part:
                items.append(part)
    return items


def _split_commas(line: str) -> List[str]:
    """Split on commas that are not inside ``{...}`` braces."""
    parts: List[str] = []
    depth = 0
    current = []
    for ch in line:
        if ch == "{":
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _expand_braces(pattern: str) -> List[str]:
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end():]
    out: List[str] = []
    for option in match.group(1).split(","):
        out.extend(_expand_braces(head + option + tail))
    return out


def _translate(pattern: str) -> str:
    i, n = 0, len(pattern)
    out: List[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                after = i + 2
                at_segment_start = i == 0 or pattern[i - 1] == "/"
                if at_segment_start and pattern.startswith("/", after):
                    # "**/" : zero or more whole directories.
                    out.append("(?:.*/)?")
                    i = after + 1
                    continue
                if at_segment_start and after == n and i > 0:
                    # "dir/**" : one or more characters below dir/.
                    out.append(".+")
                    i = after
                    continue
                out.append(".*")
                i = after
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
            else:
                body = pattern[i + 1:end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = end
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def compile_glob(pattern: str) -> List[Pattern[str]]:
    """Compile one glob (possibly with braces) into regexes over repo paths."""
    compiled: List[Pattern[str]] = []
    for pat in _expand_braces(pattern.strip()):
        if not pat:
            continue
        if pat.endswith("/"):
            pat = pat + "**"
        anchored = "/" in pat
        if pat.startswith("/"):
            pat = pat[1:]
        regex = _translate(pat)
        if not anchored:
            regex = "(?:.*/)?" + regex
        compiled.append(re.compile("^" + regex + "$"))
    return compiled


class GlobSet:
    """A set of globs; ``matches(path)`` is true when any glob matches."""

    def __init__(self, patterns: Iterable[str]):
        self.patterns: List[str] = [p for p in patterns if p and p.strip()]
        self._regexes: List[Pattern[str]] = []
        for p in self.patterns:
            self._regexes.extend(compile_glob(p))

    def matches(self, path: str) -> bool:
        path = path.lstrip("/")
        return any(r.match(path) for r in self._regexes)

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def __repr__(self) -> str:
        return f"GlobSet({self.patterns!r})"


def merge(defaults: Sequence[str], replace_raw: str | None, extra_raw: str | None) -> List[str]:
    """Resolve an input pair: ``replace`` swaps out the defaults, ``extra`` adds."""
    base = split_list(replace_raw) if replace_raw and replace_raw.strip() else list(defaults)
    for item in split_list(extra_raw):
        if item not in base:
            base.append(item)
    return base
