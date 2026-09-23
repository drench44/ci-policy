"""Test doubles shared by the unit tests."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from ci_policy import gh  # noqa: E402


class FakeGitHub:
    """Answers GET/POST by path. Values may be data, a GitHubError, or a callable."""

    def __init__(self, routes: Optional[Dict[str, Any]] = None):
        self.routes: Dict[str, Any] = dict(routes or {})
        self.calls: List[Tuple[str, str, Any]] = []
        self.posts: List[Tuple[str, Any]] = []
        # Response headers by path, for request().
        self.headers: Dict[str, Dict[str, str]] = {}

    def _answer(self, method: str, path: str, params: Any = None, body: Any = None) -> Any:
        self.calls.append((method, path, params if method == "GET" else body))
        key = f"{method} {path}"
        if key not in self.routes:
            # "METHOD /prefix/*" answers every path under that prefix.
            wild = [k for k in self.routes if k.endswith("/*") and key.startswith(k[:-1])]
            if not wild:
                raise gh.GitHubError(f"{key} -> HTTP 404: not stubbed", 404)
            key = max(wild, key=len)
        value = self.routes[key]
        if isinstance(value, Callable):  # type: ignore[arg-type]
            value = value(params if method == "GET" else body)
        if isinstance(value, gh.GitHubError):
            raise value
        return value

    def request(self, method: str, path: str, body: Any = None,
                params: Any = None) -> Tuple[int, Any, Dict[str, str]]:
        payload = self._answer(method, path, params if method == "GET" else body)
        return 200, payload, dict(self.headers.get(path, {}))

    def get(self, path: str, params: Any = None) -> Any:
        return self._answer("GET", path, params)

    def post(self, path: str, body: Any) -> Any:
        self.posts.append((path, body))
        return self._answer("POST", path, body=body)

    def paginate(self, path: str, params: Any = None, item_key: Optional[str] = None,
                 limit: int = 5000) -> List[Any]:
        payload = self._answer("GET", path, params)
        return payload.get(item_key, []) if item_key else payload


class ActionsEnv:
    """Context manager that fakes the GitHub Actions environment for main()."""

    def __init__(self, event: Dict[str, Any], inputs: Optional[Dict[str, str]] = None,
                 extra: Optional[Dict[str, str]] = None):
        self.event, self.inputs, self.extra = event, inputs or {}, extra or {}
        self._saved: Dict[str, Optional[str]] = {}

    def __enter__(self) -> "ActionsEnv":
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.event_path = os.path.join(d, "event.json")
        self.summary_path = os.path.join(d, "summary.md")
        self.output_path = os.path.join(d, "output.txt")
        with open(self.event_path, "w") as f:
            json.dump(self.event, f)
        env = {"GITHUB_EVENT_PATH": self.event_path, "GITHUB_STEP_SUMMARY": self.summary_path,
               "GITHUB_OUTPUT": self.output_path, "GITHUB_REPOSITORY": "drench44/demo",
               "GITHUB_RUN_ID": "999", "INPUT_TOKEN": "t0ken"}
        env.update({"INPUT_" + k.upper().replace("-", "_"): v for k, v in self.inputs.items()})
        env.update(self.extra)
        for k, v in env.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        # Clear inputs a previous test might have left behind.
        for k in list(os.environ):
            if k.startswith("INPUT_") and k not in env:
                self._saved[k] = os.environ.pop(k)
        return self

    @staticmethod
    def _read(path: str) -> str:
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()

    def summary(self) -> str:
        return self._read(self.summary_path)

    def outputs(self) -> str:
        return self._read(self.output_path)

    def __exit__(self, *exc: Any) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.dir.cleanup()
