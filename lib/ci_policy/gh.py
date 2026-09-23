"""Tiny GitHub REST client and GitHub Actions output helpers (stdlib only)."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class GitHubError(RuntimeError):
    """An API call failed. ``status`` is the HTTP status (0 for network errors)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class GitHub:
    """Minimal REST client: JSON in, JSON out, Link-header pagination, retries."""

    def __init__(self, token: str, api_url: str = "https://api.github.com",
                 retries: int = 3, backoff: float = 2.0):
        if not token:
            raise GitHubError("no GitHub token: pass the action's `token` input "
                              "(defaults to github.token)")
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.retries = retries
        self.backoff = backoff

    def request(self, method: str, path: str, body: Any = None,
                params: Optional[Dict[str, Any]] = None) -> Tuple[int, Any, Dict[str, str]]:
        url = path if path.startswith("http") else self.api_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        last_error: Optional[GitHubError] = None
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("User-Agent", "drench44-ci-policy")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    payload = json.loads(raw) if raw else None
                    return resp.status, payload, dict(resp.headers)
            except urllib.error.HTTPError as e:
                raw = e.read()
                try:
                    detail = json.loads(raw).get("message", "") if raw else ""
                except (ValueError, AttributeError):
                    detail = raw[:200].decode(errors="replace")
                last_error = GitHubError(f"{method} {path} -> HTTP {e.code}: {detail}", e.code)
                # Retry only what can succeed on a second try.
                if e.code < 500 and e.code != 429:
                    raise last_error
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = GitHubError(f"{method} {path} -> network error: {e}", 0)
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)
        assert last_error is not None
        raise last_error

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)[1]

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body=body)[1]

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None,
                 item_key: Optional[str] = None, limit: int = 5000) -> List[Any]:
        """Follow Link rel="next" pages. ``item_key`` unwraps object responses."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        items: List[Any] = []
        url: Optional[str] = path
        first = True
        while url and len(items) < limit:
            status, payload, headers = self.request("GET", url, params=params if first else None)
            first = False
            page = payload.get(item_key, []) if item_key else payload
            if not isinstance(page, list):
                raise GitHubError(f"GET {path}: expected a list, got {type(page).__name__}")
            items.extend(page)
            url = _next_link(headers.get("Link") or headers.get("link") or "")
        return items


def _next_link(link_header: str) -> Optional[str]:
    for part in link_header.split(","):
        m = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- Actions I/O

def _escape_data(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_prop(s: str) -> str:
    return _escape_data(s).replace(":", "%3A").replace(",", "%2C")


def annotate(level: str, message: str, title: Optional[str] = None) -> None:
    """Print a workflow command: level is notice, warning, or error."""
    props = f" title={_escape_prop(title)}" if title else ""
    print(f"::{level}{props}::{_escape_data(message)}", flush=True)


def write_summary(markdown: str) -> None:
    """Append to the job summary. Outside Actions, print it instead."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown.rstrip("\n") + "\n")
    else:
        sys.stdout.write(markdown.rstrip("\n") + "\n")


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        if "\n" in value:
            f.write(f"{name}<<CI_POLICY_EOF\n{value}\nCI_POLICY_EOF\n")
        else:
            f.write(f"{name}={value}\n")


def load_event() -> Dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        raise GitHubError("GITHUB_EVENT_PATH is not set; run this inside GitHub Actions")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def md_escape(text: str) -> str:
    """Make text safe inside a markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def env_input(name: str, default: str = "") -> str:
    """Read an input passed as INPUT_<NAME> (dashes become underscores)."""
    key = "INPUT_" + name.upper().replace("-", "_")
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def env_bool(name: str, default: bool) -> bool:
    raw = env_input(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "yes", "1", "on"):
        return True
    if raw in ("false", "no", "0", "off"):
        return False
    raise ValueError(f"input {name}: expected true or false, got {raw!r}")


def env_int(name: str, default: int) -> int:
    raw = env_input(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"input {name}: expected a whole number, got {raw!r}") from None
