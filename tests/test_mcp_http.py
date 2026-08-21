"""The HTTP boundary, driven over a real socket.

These tests speak to a live server rather than to a handler class, because the
things being checked are properties of the transport — status codes, headers,
what the socket answers to an unauthenticated caller — and a handler exercised
in isolation can pass while the server it is mounted in does not.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vikunja_claude.config import ConfigError
from vikunja_claude.mcp_server import MAX_BODY_BYTES, build_mcp_server

from .support import VIKUNJA_TOOLS, HttpTestCase, make_mcp_config


class TestAuthentication(HttpTestCase):
    def test_an_unauthenticated_request_is_refused(self):
        status, headers, _ = self.rpc("tools/list", token=None)
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))

    def test_the_refusal_points_at_the_flow_that_would_work(self):
        """RFC 9728: the 401 is how a client discovers where to get a token."""
        _, headers, _ = self.rpc("tools/list", token=None)
        challenge = headers["WWW-Authenticate"]
        self.assertIn(
            f'resource_metadata="{self.config.oauth.protected_resource_metadata_url}"',
            challenge,
        )
        self.assertIn('scope="vikunja:tickets"', challenge)

    def test_an_invalid_token_is_refused(self):
        status, _, _ = self.rpc("tools/list", token="not-a-token-this-server-issued")
        self.assertEqual(status, 401)

    def test_a_prefix_of_a_valid_token_is_refused(self):
        status, _, _ = self.rpc("tools/list", token=self.access_token()[:-1])
        self.assertEqual(status, 401)

    def test_a_refresh_token_is_not_an_access_token(self):
        status, _, _ = self.rpc(
            "tools/list", token=self.issue_tokens()["refresh_token"]
        )
        self.assertEqual(status, 401)

    def test_a_refused_request_never_reaches_vikunja(self):
        """The refusal is not a narrower version of the same surface."""
        self.rpc(
            "tools/call", {"name": "get_task", "arguments": {"task_number": 8}}, token=None
        )
        self.assertEqual(self.vikunja.calls, [])

    def test_an_oauth_token_is_accepted(self):
        status, _, text = self.rpc("tools/list")
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in json.loads(text)["result"]["tools"]}
        self.assertEqual(names, VIKUNJA_TOOLS)


class TestBrowserOriginsAreRefused(HttpTestCase):
    def test_a_request_carrying_an_origin_is_refused_even_with_a_token(self):
        status, _, _ = self.rpc("tools/list", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_it_is_refused_before_anything_is_read(self):
        self.rpc(
            "tools/call",
            {"name": "get_task", "arguments": {"task_number": 8}},
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
        status, headers, _ = self.open("/mcp", method="GET", token=None)
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_delete_is_refused(self):
        status, _, _ = self.open("/mcp", method="DELETE", token=None)
        self.assertEqual(status, 405)

    def test_an_unknown_path_is_a_404(self):
        self.assertEqual(self.open("/tasks/9", token=None)[0], 404)

    def test_posting_anywhere_else_is_a_404(self):
        status, _, _ = self.open("/tasks/9", body={"done": True}, token=None)
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
        self.assertEqual(json.loads(text)["result"]["protocolVersion"], "2025-06-18")

        status, _, text = self.rpc(
            "tools/call", {"name": "get_task", "arguments": {"task_number": 8}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["result"]["structuredContent"]["vikunja_task_id"], 9)


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
        """Refused on the declared length, before a byte of body is read.

        `declare_length` sends none, which is why this is deterministic: a
        client that streams the megabytes as well is racing the server's
        close, not testing the limit.
        """
        status, _, text = self.declare_length(2 * MAX_BODY_BYTES)
        self.assertEqual(status, 413)
        self.assertIn(str(MAX_BODY_BYTES), json.loads(text)["error"]["message"])
        self.assertEqual(self.vikunja.calls, [])

    def test_a_body_at_the_limit_is_not_refused(self):
        """The refusal is bounded by the limit, not by "large-ish"."""
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        body += " " * (MAX_BODY_BYTES - len(body))
        status, _, text = self.open(body=body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(text)["result"], {})

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
