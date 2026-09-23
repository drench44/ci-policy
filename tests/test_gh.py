import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import fakes  # noqa: F401
from ci_policy import gh


class Handler(BaseHTTPRequestHandler):
    hits = {}

    def log_message(self, *args):
        pass

    def reply(self, code, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Handler.hits[self.path] = Handler.hits.get(self.path, 0) + 1
        base = f"http://127.0.0.1:{self.server.server_port}"
        if self.path.startswith("/items?"):
            if "page=2" in self.path:
                self.reply(200, [3])
            else:
                self.reply(200, [1, 2], {"Link": f'<{base}/items?page=2>; rel="next", '
                                                  f'<{base}/items?page=2>; rel="last"'})
        elif self.path.startswith("/wrapped"):
            self.reply(200, {"total_count": 1, "check_runs": [{"id": 1}]})
        elif self.path == "/missing":
            self.reply(404, {"message": "Not Found"})
        elif self.path == "/flaky":
            if Handler.hits[self.path] < 2:
                self.reply(502, {"message": "bad gateway"})
            else:
                self.reply(200, {"ok": True})
        elif self.path == "/down":
            self.reply(500, {"message": "down"})
        else:
            self.reply(200, {"auth": self.headers.get("Authorization")})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.reply(201, {"got": json.loads(self.rfile.read(length))})


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = gh.GitHub("tok", f"http://127.0.0.1:{cls.server.server_port}", backoff=0)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_sends_token(self):
        self.assertEqual(self.api.get("/hello"), {"auth": "Bearer tok"})

    def test_paginates_link_header(self):
        self.assertEqual(self.api.paginate("/items"), [1, 2, 3])

    def test_paginate_unwraps_key(self):
        self.assertEqual(self.api.paginate("/wrapped", item_key="check_runs"), [{"id": 1}])

    def test_paginate_rejects_non_list(self):
        with self.assertRaises(gh.GitHubError):
            self.api.paginate("/hello")

    def test_404_raises_with_status_and_no_retry(self):
        with self.assertRaises(gh.GitHubError) as ctx:
            self.api.get("/missing")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("Not Found", str(ctx.exception))
        self.assertEqual(Handler.hits["/missing"], 1)

    def test_retries_5xx_then_succeeds(self):
        self.assertEqual(self.api.get("/flaky"), {"ok": True})

    def test_persistent_5xx_raises_after_retries(self):
        with self.assertRaises(gh.GitHubError) as ctx:
            self.api.get("/down")
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(Handler.hits["/down"], 3)

    def test_network_error_raises(self):
        api = gh.GitHub("tok", "http://127.0.0.1:1", retries=1)
        with self.assertRaises(gh.GitHubError) as ctx:
            api.get("/x")
        self.assertEqual(ctx.exception.status, 0)

    def test_post_sends_json(self):
        self.assertEqual(self.api.post("/issues", {"a": 1}), {"got": {"a": 1}})

    def test_missing_token_is_an_error(self):
        with self.assertRaises(gh.GitHubError):
            gh.GitHub("")


class ActionsIOTests(unittest.TestCase):
    def test_annotate_escapes(self):
        with mock.patch("builtins.print") as p:
            gh.annotate("error", "50% done\nnext", "a:b,c")
        self.assertEqual(p.call_args[0][0], "::error title=a%3Ab%2Cc::50%25 done%0Anext")

    def test_outputs_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            out, summ = os.path.join(d, "o"), os.path.join(d, "s")
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out, "GITHUB_STEP_SUMMARY": summ}):
                gh.set_output("result", "pass")
                gh.set_output("multi", "a\nb")
                gh.write_summary("# hi")
            with open(out) as f:
                text = f.read()
            self.assertIn("result=pass\n", text)
            self.assertIn("multi<<CI_POLICY_EOF\na\nb\nCI_POLICY_EOF\n", text)
            with open(summ) as f:
                self.assertEqual(f.read(), "# hi\n")

    def test_env_helpers(self):
        with mock.patch.dict(os.environ, {"INPUT_SIZE_WARN": "12", "INPUT_FLAG": "no",
                                          "INPUT_BAD": "maybe"}):
            self.assertEqual(gh.env_int("size-warn", 1), 12)
            self.assertFalse(gh.env_bool("flag", True))
            self.assertTrue(gh.env_bool("unset-thing", True))
            with self.assertRaises(ValueError):
                gh.env_bool("bad", True)

    def test_md_escape(self):
        self.assertEqual(gh.md_escape("a|b\nc"), "a\\|b c")


if __name__ == "__main__":
    unittest.main()
