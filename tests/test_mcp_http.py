"""The HTTP boundary, driven over a real socket.

These tests speak to a live server rather than to a handler class, because the
things being checked are properties of the transport — status codes, headers,
what the socket answers to an unauthenticated caller — and a handler exercised
in isolation can pass while the server it is mounted in does not.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from vikunja_claude.config import ConfigError
from vikunja_claude.mcp_server import build_mcp_server
from vikunja_claude.vikunja import VikunjaClient

from .fakes import PROJECT_ID, FakeVikunja
from .support import MCP_TOKEN, make_mcp_config


class HttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = make_mcp_config(Path(self._tmp.name))
        self.vikunja = FakeVikunja()
        client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.server = build_mcp_server(self.config, client=client)
        self.addCleanup(self.server.server_close)
        # A short poll interval only so shutdown() is prompt: the default 0.5s
        # is per test, not per request, and dominates the run.
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.origin = "http://127.0.0.1:%d" % self.server.server_address[1]

    # -- helpers -----------------------------------------------------------

    def open(
        self,
        path: str = "/mcp",
        body: dict | str | None = None,
        method: str | None = None,
        token: str | None = MCP_TOKEN,
        accept: str = "application/json",
        headers: dict | None = None,
    ):
        """Returns (status, headers, body-text). Never raises on 4xx/5xx."""
        payload = None
        if body is not None:
            payload = (body if isinstance(body, str) else json.dumps(body)).encode()
        request = urllib.request.Request(
            self.origin + path,
            data=payload,
            method=method or ("POST" if payload is not None else "GET"),
        )
        request.add_header("Accept", accept)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        if token is not None:
            request.add_header("Authorization", f"Bearer {token}")
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode()

    def rpc(self, method: str, params: dict | None = None, **kwargs):
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        status, headers, text = self.open(body=message, **kwargs)
        return status, headers, text


class TestAuthentication(HttpTestCase):
    def test_an_unauthenticated_request_is_refused(self):
        status, headers, _ = self.rpc("tools/list", token=None)
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))

    def test_a_wrong_token_is_refused(self):
        status, _, _ = self.rpc("tools/list", token="not-the-token")
        self.assertEqual(status, 401)

    def test_a_prefix_of_the_token_is_refused(self):
        status, _, _ = self.rpc("tools/list", token=MCP_TOKEN[:-1])
        self.assertEqual(status, 401)

    def test_a_refused_request_never_reaches_vikunja(self):
        """The refusal is not a narrower version of the same surface."""
        self.rpc("tools/call", {"name": "get_task", "arguments": {"task_id": 9}}, token=None)
        self.assertEqual(self.vikunja.calls, [])

    def test_the_right_token_is_accepted(self):
        status, _, text = self.rpc("tools/list")
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in json.loads(text)["result"]["tools"]}
        self.assertEqual(names, {"get_task", "create_task"})


class TestBrowserOriginsAreRefused(HttpTestCase):
    def test_a_request_carrying_an_origin_is_refused_even_with_the_token(self):
        status, _, _ = self.rpc("tools/list", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_it_is_refused_before_anything_is_read(self):
        self.rpc(
            "tools/call",
            {"name": "get_task", "arguments": {"task_id": 9}},
            headers={"Origin": "http://127.0.0.1:3456"},
        )
        self.assertEqual(self.vikunja.calls, [])


class TestRoutes(HttpTestCase):
    def test_health_needs_no_token(self):
        status, _, text = self.open("/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["status"], "ok")

    def test_health_says_nothing_about_the_board(self):
        """It is reachable without the credential, so it may only prove liveness."""
        _, _, text = self.open("/health", token=None)
        body = json.loads(text)
        self.assertEqual(set(body), {"status", "service"})
        self.assertNotIn("AI Alpha Engine", text)

    def test_there_is_no_server_initiated_stream(self):
        status, headers, _ = self.open("/mcp", method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_delete_is_refused(self):
        status, _, _ = self.open("/mcp", method="DELETE")
        self.assertEqual(status, 405)

    def test_an_unknown_path_is_a_404(self):
        self.assertEqual(self.open("/tasks/9")[0], 404)

    def test_posting_anywhere_else_is_a_404(self):
        status, _, _ = self.open("/tasks/9", body={"done": True})
        self.assertEqual(status, 404)


class TestFraming(HttpTestCase):
    def test_json_is_the_default(self):
        status, headers, text = self.rpc("ping")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(text)["result"], {})

    def test_a_client_asking_for_sse_gets_an_sse_event(self):
        status, headers, text = self.rpc("ping", accept="text/event-stream")
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["Content-Type"])
        self.assertTrue(text.startswith("event: message\ndata: "))
        payload = json.loads(text.split("data: ", 1)[1].strip())
        self.assertEqual(payload["result"], {})

    def test_a_notification_is_accepted_with_no_body(self):
        status, _, text = self.open(
            body={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(text, "")

    def test_a_full_handshake_and_call_works_over_http(self):
        status, _, text = self.rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(text)["result"]["protocolVersion"], "2025-06-18"
        )

        status, _, text = self.rpc(
            "tools/call", {"name": "get_task", "arguments": {"task_id": 9}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(text)["result"]["structuredContent"]["task_id"], 9
        )


class TestMalformedRequests(HttpTestCase):
    def test_a_body_that_is_not_json_is_a_400(self):
        status, _, _ = self.open(body="{not json")
        self.assertEqual(status, 400)

    def test_a_batch_is_a_400(self):
        status, _, text = self.open(
            body=json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        )
        self.assertEqual(status, 400)
        self.assertIn("batch", json.loads(text)["error"]["message"].lower())

    def test_an_oversized_body_is_refused(self):
        huge = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "create_task",
                "arguments": {
                    "project_id": PROJECT_ID,
                    "title": "x",
                    "description": "y" * (2 * 1024 * 1024),
                },
            },
        }
        status, _, _ = self.open(body=huge)
        self.assertEqual(status, 413)
        self.assertEqual(self.vikunja.calls, [])

    def test_a_failure_never_returns_a_traceback(self):
        status, _, text = self.open(body="{not json")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("vikunja_claude/", text)
        self.assertNotIn(self.config.token, text)


class TestBindGuard(unittest.TestCase):
    def test_it_refuses_to_bind_off_loopback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_mcp_config(Path(tmp), host="0.0.0.0")
            with self.assertRaises(ConfigError) as caught:
                build_mcp_server(config)
        self.assertIn("Refusing to bind", str(caught.exception))

    def test_loopback_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_mcp_config(Path(tmp))
            server = build_mcp_server(config)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
