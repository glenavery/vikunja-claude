"""The authenticated page read (task 239): what it can ask for, and as whom.

This is the one tool on this boundary that sees a page a stranger cannot, so
what is asserted here is everything that keeps it narrow.

* **Off means absent.** Unconfigured, the tool is not advertised and cannot be
  called — the same rule the operational reads and the public fetch hold.
* **The identity is not an argument.** The tool takes a path and nothing else,
  the schema refuses any other property, and no user id appears anywhere between
  the tool and the request. Who the page is read as is decided by the
  application, from its own configuration.
* **It cannot be aimed.** One endpoint, named by a constant; the caller's path is
  a query argument to it, and a value that carries a scheme, an authority, a
  backslash or a fragment is refused before anything is sent.
* **The answer is projected, not passed through.** A field the application grows
  later reaches nobody until it is named here.
* **A refusal is a refusal.** The application's own 400 and 503 explanations are
  repeated verbatim, because they say which route was refused or which setting
  is missing; nothing is reported as a page.

The wire test runs against a real ``http.server`` rather than a mocked
``urlopen``, because "the request is a GET carrying the API key and nothing
else" is a property of the request urllib builds, and a mock would assert the
mock.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from vikunja_claude.mcp import McpProtocol
from vikunja_claude.mcp_service import McpService
from vikunja_claude.paying_page import (
    PATH_TEST_PAYING_PAGE,
    RESULT_FIELDS,
    PayingSiteClient,
    PayingPageError,
)
from vikunja_claude.vikunja import VikunjaClient

from .fakes import FakeVikunja
from .support import (
    INVESTMENT_API_KEY,
    PAYING_PAGE_TOOLS,
    VIKUNJA_TOOLS,
    make_investment_config,
    make_mcp_config,
)

TOOL = "fetch_test_paying_page"

PAGE_ANSWER = {
    "url": "http://127.0.0.1:8001/cockpit/example",
    "path": "/cockpit/example",
    "status": 200,
    "status_text": "OK",
    "headers": {"content-type": "text/html; charset=utf-8"},
    "set_cookie_names": ["session"],
    "html": "<html><body><h1>Paying report</h1></body></html>",
    "html_omitted_reason": None,
    "truncated": False,
    "bytes": 48,
    "authenticated": True,
    "identity": {"user_id": 7, "access_level": "paying"},
}


class RecordingApi:
    """A stand-in for the investment admin API, recording the URLs it was given."""

    def __init__(self, answer=None, fail: Exception | None = None):
        self.answer = PAGE_ANSWER if answer is None else answer
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        return self.answer

    @property
    def queries(self) -> list[dict[str, list[str]]]:
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(u).query) for u in self.urls]


class PayingPageTestCase(unittest.TestCase):
    """A service with the investment settings on, wired to a recorder."""

    answer = None
    api_fail: Exception | None = None

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = make_mcp_config(
            Path(tmp.name), investment=make_investment_config()
        )
        self.recorder = RecordingApi(answer=self.answer, fail=self.api_fail)
        self.service = McpService(
            self.config,
            VikunjaClient(self.config.api_url, self.config.token, transport=FakeVikunja()),
            paying_site=PayingSiteClient(
                self.config.investment.api_url,
                self.config.investment.api_key,
                transport=self.recorder,
            ),
        )
        self.protocol = McpProtocol(self.service.tools())

    def call(self, **arguments) -> dict:
        response = self.protocol.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL, "arguments": arguments},
            }
        )
        assert response is not None
        return response

    def structured(self, **arguments) -> dict:
        response = self.call(**arguments)
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertNotIn("isError", result, result)
        return result["structuredContent"]

    def error_text(self, **arguments) -> str:
        response = self.call(**arguments)
        result = response.get("result", {})
        self.assertTrue(result.get("isError"), response)
        return " ".join(part.get("text", "") for part in result.get("content", []))


# ── off means absent ─────────────────────────────────────────────────────────

class TestTheDisabledState(unittest.TestCase):
    def _service(self, investment):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name), investment=investment)
        return McpService(
            config,
            VikunjaClient(config.api_url, config.token, transport=FakeVikunja()),
        )

    def test_unconfigured_the_tool_is_not_advertised(self):
        service = self._service(None)
        names = {tool.name for tool in service.tools()}
        self.assertEqual(names, VIKUNJA_TOOLS)
        self.assertFalse(names & PAYING_PAGE_TOOLS)
        self.assertFalse(service.paying_page_fetch_enabled)

    def test_configured_it_is(self):
        service = self._service(make_investment_config())
        names = {tool.name for tool in service.tools()}
        self.assertTrue(PAYING_PAGE_TOOLS <= names)
        self.assertTrue(service.paying_page_fetch_enabled)

    def test_an_unadvertised_tool_cannot_be_called_anyway(self):
        protocol = McpProtocol(self._service(None).tools())
        response = protocol.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": TOOL, "arguments": {"path": "/"}},
            }
        )
        self.assertIn("error", response)

    def test_the_client_is_built_from_configuration(self):
        """Not only injected in tests: the live service builds its own."""
        service = self._service(make_investment_config())
        self.assertIsNotNone(service.paying_site)
        self.assertEqual(
            service.paying_site.api_url, make_investment_config().api_url
        )


# ── the identity is not an argument ──────────────────────────────────────────

class TestTheIdentityIsNotAnArgument(PayingPageTestCase):
    def test_the_schema_accepts_a_path_and_nothing_else(self):
        (tool,) = [t for t in self.service.tools() if t.name == TOOL]
        self.assertEqual(set(tool.input_schema["properties"]), {"path"})
        self.assertEqual(tool.input_schema["required"], ["path"])
        self.assertIs(tool.input_schema["additionalProperties"], False)

    def test_a_supplied_user_changes_nothing(self):
        """Impossible rather than refused: there is nowhere for it to go.

        The schema declares ``additionalProperties: false``, but a client is
        free to send more anyway — so the guarantee that matters is not that the
        extra argument is rejected, it is that the request built from it is
        identical. The tool reads ``path`` and nothing else, and the identity is
        never a value this process holds.
        """
        for extra in ({"user_id": 1}, {"user": "admin"}, {"as_user": 7}):
            with self.subTest(extra=extra):
                self.recorder.urls.clear()
                self.structured(path="/cockpit/example", **extra)
                (query,) = self.recorder.queries
                self.assertEqual(query, {"path": ["/cockpit/example"]})

    def test_no_identity_is_sent_with_the_request(self):
        """The request names a path. Who it is read as is the server's business."""
        self.structured(path="/cockpit/example")
        (query,) = self.recorder.queries
        self.assertEqual(set(query), {"path"})
        self.assertEqual(query["path"], ["/cockpit/example"])

    def test_the_service_method_takes_only_a_path(self):
        import inspect

        parameters = list(inspect.signature(self.service.fetch_test_paying_page).parameters)
        self.assertEqual(parameters, ["path"])


# ── it cannot be aimed ───────────────────────────────────────────────────────

class TestItCannotBeAimed(PayingPageTestCase):
    def test_one_endpoint_and_the_path_is_a_query_argument(self):
        self.structured(path="/report/example?lang=sv")
        (url,) = self.recorder.urls
        self.assertTrue(
            url.startswith(
                make_investment_config().api_url + PATH_TEST_PAYING_PAGE + "?"
            ),
            url,
        )
        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["path"],
            ["/report/example?lang=sv"],
        )

    def test_a_path_that_names_another_host_is_refused_before_anything_is_sent(self):
        for path in (
            "https://example.com/",
            "//example.com/x",
            "/\\evil.com",
            "/about#top",
            "",
            "   ",
            "about",
            "/about page",
        ):
            with self.subTest(path=path):
                text = self.error_text(path=path)
                self.assertTrue(text.strip())
                self.assertEqual(self.recorder.urls, [])

    def test_a_path_is_encoded_rather_than_concatenated(self):
        """A path with an ampersand cannot add a second query parameter."""
        self.structured(path="/report/example?a=1&path=/admin/users")
        (query,) = self.recorder.queries
        self.assertEqual(query["path"], ["/report/example?a=1&path=/admin/users"])
        self.assertEqual(set(query), {"path"})


# ── the answer ───────────────────────────────────────────────────────────────

class TestTheAnswer(PayingPageTestCase):
    def test_the_page_comes_back_whole(self):
        answer = self.structured(path="/cockpit/example")
        self.assertEqual(answer["status"], 200)
        self.assertIn("Paying report", answer["html"])
        self.assertIs(answer["authenticated"], True)
        self.assertEqual(answer["identity"], {"user_id": 7, "access_level": "paying"})

    def test_every_field_is_named_and_nothing_else_is(self):
        answer = self.structured(path="/cockpit/example")
        self.assertEqual(set(answer), set(RESULT_FIELDS))

    def test_an_authenticated_read_is_distinguishable_from_an_anonymous_one(self):
        """`authenticated` is what tells the two fetches apart in an answer."""
        answer = self.structured(path="/cockpit/example")
        self.assertIs(answer["authenticated"], True)
        self.assertIn("identity", answer)


class TestAFieldNobodyDecidedOn(PayingPageTestCase):
    answer = dict(PAGE_ANSWER, session_cookie="s3cret", request_headers={"Cookie": "x"})

    def test_a_field_the_application_grows_is_not_published(self):
        answer = self.structured(path="/cockpit/example")
        self.assertEqual(set(answer), set(RESULT_FIELDS))
        serialised = json.dumps(answer)
        self.assertNotIn("s3cret", serialised)
        self.assertNotIn("Cookie", serialised)


class TestNoCredentialMaterial(PayingPageTestCase):
    def test_the_api_key_is_never_in_the_answer(self):
        answer = self.structured(path="/cockpit/example")
        self.assertNotIn(INVESTMENT_API_KEY, json.dumps(answer))


# ── at the wire, and the failures that live there ────────────────────────────
#
# Every failure below is raised by the client's own transport, so each one is
# driven against a real ``http.server`` answering a real status code. Injecting
# an exception into the transport would skip the code that turns an HTTP failure
# into a sentence, which is the part being asserted.

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _record(self):
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )

    def do_GET(self):  # noqa: N802 — the name http.server requires
        self._record()
        status, payload = self.server.answer  # type: ignore[attr-defined]
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self._record()
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class WireTestCase(unittest.TestCase):
    """A client pointed at a real server that answers ``self.answers``."""

    answers: tuple = (200, PAGE_ANSWER)

    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []
        self.server.answer = self.answers
        thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.client = PayingSiteClient(
            "http://127.0.0.1:%d" % self.port, INVESTMENT_API_KEY, timeout=5
        )

    def refusal(self, path: str = "/cockpit/example") -> str:
        with self.assertRaises(PayingPageError) as caught:
            self.client.fetch_page(path)
        return str(caught.exception)


class TestTheRequestOnTheWire(WireTestCase):
    def test_it_is_a_get_carrying_the_api_key(self):
        answer = self.client.fetch_page("/cockpit/example")

        self.assertEqual(answer["status"], 200)
        (request,) = self.server.requests
        self.assertEqual(request["method"], "GET")
        self.assertTrue(request["path"].startswith(PATH_TEST_PAYING_PAGE + "?"))
        self.assertEqual(request["headers"]["x-api-key"], INVESTMENT_API_KEY)
        self.assertNotIn("cookie", request["headers"])
        self.assertNotIn("authorization", request["headers"])

    def test_the_client_has_no_way_to_write(self):
        """Not a refused write method — an absent one."""
        for forbidden in ("post", "put", "patch", "delete", "create", "update"):
            self.assertFalse(
                [name for name in dir(self.client) if forbidden in name.lower()],
                forbidden,
            )

    def test_a_bad_path_never_reaches_the_wire(self):
        with self.assertRaises(PayingPageError):
            self.client.fetch_page("https://example.com/")
        self.assertEqual(self.server.requests, [])


class TestARefusalIsRepeated(WireTestCase):
    answers = (
        400,
        {
            "detail": "/logout is not readable through this tool because it "
            "manages authentication. Nothing was read."
        },
    )

    def test_the_applications_refusal_reaches_the_caller(self):
        """The application decided; its reason is what the caller needs."""
        self.assertIn("manages authentication", self.refusal("/logout"))


class TestAnUnconfiguredApplication(WireTestCase):
    answers = (
        503,
        {"detail": "TEST_PAYING_USER_ID is not set on this instance. Nothing was read."},
    )

    def test_the_missing_setting_is_named(self):
        text = self.refusal()
        self.assertIn("TEST_PAYING_USER_ID", text)
        self.assertIn("Nothing was read", text)


class TestAnIdentityThatIsNoLongerPaying(WireTestCase):
    answers = (
        503,
        {
            "detail": "TEST_PAYING_USER_ID names user 7, whose effective access "
            "level is 'public' and not 'paying'. Nothing was read."
        },
    )

    def test_it_is_a_refusal_and_never_a_page(self):
        text = self.refusal()
        self.assertIn("not 'paying'", text)
        self.assertIn("Nothing was read", text)


class TestARejectedKey(WireTestCase):
    answers = (403, {"detail": "Invalid API key"})

    def test_it_reads_as_configuration_and_not_as_a_page(self):
        text = self.refusal()
        self.assertIn("INVESTMENT_API_KEY", text)
        self.assertIn("Nothing was read", text)


class TestAMissingEndpoint(WireTestCase):
    answers = (404, None)

    def test_both_causes_are_named(self):
        text = self.refusal()
        self.assertIn("public instance", text)
        self.assertIn("restarted", text)


class TestANonObjectAnswer(WireTestCase):
    answers = (200, ["not", "an", "object"])

    def test_it_is_refused_rather_than_reshaped(self):
        self.assertIn("not an object", self.refusal())


class TestAnEmptyAnswer(WireTestCase):
    answers = (200, None)

    def test_it_is_refused_rather_than_read_as_an_empty_page(self):
        self.assertIn("empty body", self.refusal())


class TestAnUnreachableApi(WireTestCase):
    def test_nothing_is_reported_as_read(self):
        self.server.shutdown()
        self.server.server_close()
        text = self.refusal()
        self.assertIn("Cannot reach the investment API", text)
        self.assertIn("Nothing was read", text)
