"""The public page fetch (task 204): what it reads, and what it cannot reach.

Four properties are asserted here that no other test in the tree covers.

* **Off means absent.** Unconfigured, the tool is not advertised at all — the
  same rule the operational reads hold, from its own setting.
* **It cannot be aimed.** The site comes from configuration; the argument is a
  path, and every form of it that would name another host is refused.
* **It carries no credential.** No cookie, no API key, no session — asserted at
  the wire, against a real server, and across two calls so a cookie handed back
  by the first cannot be presented by the second. This is what makes "it cannot
  read a page a stranger cannot read" arithmetic rather than a promise.
* **A refusal is returned, not followed.** The 303 to the login page is the
  answer; following it would replace the evidence of the refusal with a page.

Most of it runs against a real ``http.server`` rather than a mocked
``urlopen``, because the two things most worth proving — that no redirect is
followed and that no cookie comes back — are behaviours of urllib's opener, and
a mock would assert the fake instead.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from vikunja_claude.config import ConfigError, McpConfig
from vikunja_claude.mcp import McpProtocol, ToolError
from vikunja_claude.mcp_service import McpService
from vikunja_claude.vikunja import VikunjaClient
from vikunja_claude.website import (
    MAX_BODY_BYTES,
    MAX_PATH_CHARS,
    PublicPageError,
    PublicSiteClient,
    RawPage,
    normalise_path,
)

from .fakes import FakeVikunja
from .support import (
    ISSUER,
    INVESTMENT_TOOLS,
    OPERATIONAL_TOOLS,
    PASSPHRASE,
    PUBLIC_SITE_URL,
    TOKEN,
    VIKUNJA_TOOLS,
    WEBSITE_TOOLS,
    make_investment_config,
    make_mcp_config,
)

HOME_HTML = "<html><head><title>AI Alpha Engine</title></head><body>Hej</body></html>"


class _SiteHandler(BaseHTTPRequestHandler):
    """A stand-in for the public instance: a few pages with fixed answers."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 — the name http.server requires
        self.server.requests.append(  # type: ignore[attr-defined]
            {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}}
        )
        route = self.path.split("?", 1)[0]
        if route == "/":
            self._send(200, "text/html; charset=utf-8", HOME_HTML.encode("utf-8"))
        elif route == "/protected":
            # What the application does to an anonymous visitor: sends them to
            # the login page, and hands out a session cookie on the way.
            self._send(
                303,
                "text/html; charset=utf-8",
                b"",
                extra=[
                    ("Location", "/login"),
                    ("Set-Cookie", "session=super-secret-value; HttpOnly; Path=/"),
                ],
            )
        elif route == "/big":
            self._send(200, "text/html", b"x" * (MAX_BODY_BYTES + 500))
        elif route == "/logo.png":
            self._send(200, "image/png", b"\x89PNG\r\n\x1a\n" + b"\xff" * 32)
        elif route == "/two-links":
            self._send(
                200,
                "text/html",
                b"ok",
                extra=[
                    ("Link", '<https://example.com/en>; rel="alternate"'),
                    ("Link", '<https://example.com/sv>; rel="alternate"'),
                ],
            )
        elif route == "/latin":
            self._send(200, "text/html; charset=latin-1", "Ångström".encode("latin-1"))
        else:
            self._send(404, "text/html; charset=utf-8", b"<html>not found</html>")

    def do_POST(self):  # noqa: N802
        self.server.requests.append({"path": self.path, "method": "POST"})  # type: ignore[attr-defined]
        self._send(405, "text/plain", b"no")

    def _send(self, status, content_type, body, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — the signature it overrides
        pass


class SiteTestCase(unittest.TestCase):
    """A client pointed at a real, local, throwaway site."""

    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
        self.server.requests = []  # type: ignore[attr-defined]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address[:2]
        self.site_url = f"http://{host}:{port}"
        self.client = PublicSiteClient(self.site_url)

    @property
    def requests(self) -> list[dict]:
        return self.server.requests  # type: ignore[attr-defined]


# ── off means absent ─────────────────────────────────────────────────────────

class TestTheDisabledState(unittest.TestCase):
    def _service(self, **overrides):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name), **overrides)
        return McpService(
            config,
            VikunjaClient(config.api_url, config.token, transport=FakeVikunja()),
        )

    def test_unconfigured_the_page_tool_is_not_advertised(self):
        service = self._service()
        names = {tool.name for tool in service.tools()}
        self.assertEqual(names, VIKUNJA_TOOLS)
        self.assertFalse(names & WEBSITE_TOOLS)
        self.assertFalse(service.page_fetch_enabled)

    def test_configured_it_is(self):
        service = self._service(public_site_url=PUBLIC_SITE_URL)
        names = {tool.name for tool in service.tools()}
        self.assertEqual(names, VIKUNJA_TOOLS | WEBSITE_TOOLS)
        self.assertTrue(service.page_fetch_enabled)

    def test_it_is_independent_of_the_operational_reads(self):
        """Two settings, two capabilities. Either alone must work."""
        page_only = {
            tool.name for tool in self._service(public_site_url=PUBLIC_SITE_URL).tools()
        }
        reads_only = {
            tool.name
            for tool in self._service(investment=make_investment_config()).tools()
        }
        both = {
            tool.name
            for tool in self._service(
                investment=make_investment_config(), public_site_url=PUBLIC_SITE_URL
            ).tools()
        }
        self.assertEqual(page_only, VIKUNJA_TOOLS | WEBSITE_TOOLS)
        # INVESTMENT_TOOLS, not OPERATIONAL_TOOLS: the investment settings
        # switch on the three reads and the authenticated page read (task
        # 239) together. What this test is about is unchanged — the public
        # page fetch is still its own setting, and neither side pulls in
        # the other.
        self.assertEqual(reads_only, VIKUNJA_TOOLS | INVESTMENT_TOOLS)
        self.assertEqual(both, VIKUNJA_TOOLS | INVESTMENT_TOOLS | WEBSITE_TOOLS)

    def test_an_unadvertised_tool_cannot_be_called_anyway(self):
        protocol = McpProtocol(self._service().tools())
        response = protocol.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "fetch_public_page", "arguments": {"path": "/"}},
            }
        )
        self.assertIn("error", response)

    def test_calling_it_with_no_configuration_names_the_setting(self):
        """Reached only if a tool was called that should not have been advertised."""
        with self.assertRaises(ToolError) as caught:
            self._service().fetch_public_page("/")
        self.assertIn("INVESTMENT_PUBLIC_URL", str(caught.exception))
        self.assertIn("nothing is known", str(caught.exception))

    def test_a_service_built_without_a_client_but_with_config_still_fetches(self):
        service = self._service(public_site_url=PUBLIC_SITE_URL)
        self.assertIsNotNone(service.site)
        self.assertEqual(service.site.site_url, PUBLIC_SITE_URL)


# ── the fetch ────────────────────────────────────────────────────────────────

class TestWhatComesBack(SiteTestCase):
    def test_the_page_arrives_with_its_status_and_headers(self):
        page = self.client.fetch_page("/")
        self.assertEqual(page["status"], 200)
        self.assertEqual(page["html"], HOME_HTML)
        self.assertEqual(page["headers"]["content-type"], "text/html; charset=utf-8")
        self.assertEqual(page["url"], self.site_url + "/")
        self.assertIs(page["truncated"], False)
        self.assertIsNone(page["html_omitted_reason"])

    def test_a_query_string_is_carried_through(self):
        """Which is how the Swedish site is reviewed at all (task 201)."""
        self.client.fetch_page("/?lang=sv")
        self.assertEqual(self.requests[-1]["path"], "/?lang=sv")

    def test_a_404_is_an_answer_not_a_failure(self):
        page = self.client.fetch_page("/no-such-page")
        self.assertEqual(page["status"], 404)
        self.assertIn("not found", page["html"])

    def test_repeated_headers_are_kept_rather_than_overwritten(self):
        page = self.client.fetch_page("/two-links")
        self.assertIn("/en", page["headers"]["link"])
        self.assertIn("/sv", page["headers"]["link"])

    def test_a_declared_charset_is_honoured(self):
        page = self.client.fetch_page("/latin")
        self.assertEqual(page["html"], "Ångström")

    def test_an_oversized_body_is_cut_and_says_so(self):
        page = self.client.fetch_page("/big")
        self.assertIs(page["truncated"], True)
        self.assertEqual(len(page["html"]), MAX_BODY_BYTES)

    def test_a_binary_body_is_omitted_with_its_reason(self):
        page = self.client.fetch_page("/logo.png")
        self.assertEqual(page["status"], 200)
        self.assertIsNone(page["html"])
        self.assertIn("image/png", page["html_omitted_reason"])

    def test_an_unreachable_site_says_nothing_was_fetched(self):
        client = PublicSiteClient("http://127.0.0.1:1")
        with self.assertRaises(PublicPageError) as caught:
            client.fetch_page("/")
        self.assertIn("Nothing was fetched", str(caught.exception))


# ── it carries no credential ─────────────────────────────────────────────────

class TestTheAnonymousVisitor(SiteTestCase):
    def test_the_request_carries_no_credential_of_any_kind(self):
        self.client.fetch_page("/")
        headers = self.requests[-1]["headers"]
        for name in ("cookie", "authorization", "x-api-key"):
            self.assertNotIn(name, headers, f"{name} was sent")

    def test_a_cookie_handed_back_is_not_presented_on_the_next_fetch(self):
        """No cookie jar: two fetches are two visitors, not one session."""
        first = self.client.fetch_page("/protected")
        self.assertEqual(first["set_cookie_names"], ["session"])
        self.client.fetch_page("/")
        self.assertNotIn("cookie", self.requests[-1]["headers"])

    def test_the_cookie_value_is_never_returned(self):
        page = self.client.fetch_page("/protected")
        self.assertNotIn("set-cookie", page["headers"])
        self.assertNotIn("super-secret-value", str(page))
        self.assertEqual(page["set_cookie_names"], ["session"])

    def test_a_protected_page_answers_with_its_redirect_and_it_is_not_followed(self):
        page = self.client.fetch_page("/protected")
        self.assertEqual(page["status"], 303)
        self.assertEqual(page["headers"]["location"], "/login")
        self.assertIs(page["authenticated"], False)
        # The login page was never requested: the refusal is what came back.
        self.assertEqual([r["path"] for r in self.requests], ["/protected"])

    def test_the_client_takes_no_key_and_holds_none(self):
        """Absent, not withheld."""
        import inspect

        parameters = set(inspect.signature(PublicSiteClient.__init__).parameters)
        self.assertEqual(parameters, {"self", "site_url", "transport", "timeout"})
        self.assertFalse(
            [name for name in vars(self.client) if "key" in name or "token" in name]
        )

    def test_the_client_exposes_one_read_and_no_write(self):
        public = {name for name in dir(self.client) if not name.startswith("_")}
        self.assertEqual(public, {"fetch_page", "site_url"})
        for verb in ("post", "put", "patch", "delete", "submit"):
            self.assertFalse(
                hasattr(PublicSiteClient, verb), f"PublicSiteClient.{verb} exists"
            )

    def test_every_request_is_a_get(self):
        self.client.fetch_page("/")
        self.client.fetch_page("/protected")
        self.assertNotIn("POST", [r.get("method") for r in self.requests])
        self.assertEqual(len(self.requests), 2)


# ── it cannot be aimed ───────────────────────────────────────────────────────

class TestPathsThatAreRefused(unittest.TestCase):
    def refusal(self, path) -> str:
        with self.assertRaises(PublicPageError) as caught:
            normalise_path(path)
        return str(caught.exception)

    def test_a_full_url_is_refused(self):
        self.assertIn("must start", self.refusal("https://example.com/x"))

    def test_a_protocol_relative_path_is_refused(self):
        """`//example.com/x` names another host."""
        self.assertIn("another host", self.refusal("//example.com/x"))

    def test_a_backslash_is_refused(self):
        """A browser reads it as a slash; urllib does not."""
        self.assertIn("backslash", self.refusal("/\\example.com"))

    def test_a_fragment_is_refused(self):
        self.assertIn("fragment", self.refusal("/about#top"))

    def test_a_bare_word_is_refused(self):
        self.assertIn("must start", self.refusal("about"))

    def test_whitespace_and_control_characters_are_refused(self):
        self.assertIn("whitespace", self.refusal("/a b"))
        self.assertIn("whitespace", self.refusal("/a\nb"))

    def test_an_empty_path_is_refused_with_the_answer(self):
        self.assertIn('"/"', self.refusal(""))

    def test_something_that_is_not_a_string_is_refused(self):
        self.assertIn("must be a string", self.refusal(7))

    def test_an_absurdly_long_path_is_refused(self):
        self.assertIn(str(MAX_PATH_CHARS), self.refusal("/" + "a" * MAX_PATH_CHARS))

    def test_the_paths_a_reviewer_actually_uses_are_accepted(self):
        for path in ("/", "/about", "/how-it-works", "/terms?lang=sv", "/a/b/c"):
            self.assertEqual(normalise_path(path), path)

    def test_the_host_comes_from_configuration_and_the_path_from_the_caller(self):
        recorded = []

        def transport(url):
            recorded.append(url)
            return RawPage(status=200, reason="OK", headers=[], body=b"")

        client = PublicSiteClient("http://127.0.0.1:8001", transport=transport)
        client.fetch_page("/about")
        self.assertEqual(recorded, ["http://127.0.0.1:8001/about"])


# ── through the tool ─────────────────────────────────────────────────────────

class TestTheTool(SiteTestCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name), public_site_url=self.site_url)
        self.vikunja = FakeVikunja()
        self.service = McpService(
            config,
            VikunjaClient(config.api_url, config.token, transport=self.vikunja),
        )
        self.protocol = McpProtocol(self.service.tools())
        self.tool = next(
            tool for tool in self.service.tools() if tool.name == "fetch_public_page"
        )

    def call(self, **arguments) -> dict:
        return self.protocol.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "fetch_public_page", "arguments": arguments},
            }
        )

    def test_a_page_comes_back_through_the_protocol(self):
        response = self.call(path="/")
        self.assertNotIn("error", response)
        result = response["result"]
        self.assertNotIn("isError", result)
        self.assertEqual(result["structuredContent"]["status"], 200)
        self.assertIn("AI Alpha Engine", result["structuredContent"]["html"])

    def test_a_refused_path_is_a_tool_error_not_a_crash(self):
        result = self.call(path="https://example.com")["result"]
        self.assertTrue(result.get("isError"), result)
        self.assertIn("must start", result["content"][0]["text"])

    def test_the_path_is_required(self):
        self.assertEqual(self.tool.required_arguments(), ["path"])
        self.assertIn("error", self.call())

    def test_it_is_marked_read_only(self):
        self.assertIs(self.tool.annotations["readOnlyHint"], True)
        self.assertIs(self.tool.input_schema["additionalProperties"], False)
        self.assertEqual(set(self.tool.input_schema["properties"]), {"path"})

    def test_fetching_a_page_touches_no_task(self):
        self.call(path="/")
        self.assertEqual(self.vikunja.calls, [])


# ── configuration ────────────────────────────────────────────────────────────

class TestPublicSiteConfig(unittest.TestCase):
    def _from_env(self, public=None, admin=None, key=None) -> McpConfig:
        import os

        names = (
            "INVESTMENT_PUBLIC_URL",
            "INVESTMENT_API_URL",
            "INVESTMENT_API_KEY",
            "VIKUNJA_API_TOKEN",
            "VIKUNJA_MCP_OAUTH_ISSUER",
            "VIKUNJA_MCP_OAUTH_PASSPHRASE",
        )
        saved = {name: os.environ.pop(name, None) for name in names}
        try:
            # The whole config is built, not just this setting, so these say the
            # value reaches `McpConfig` rather than that a helper returns it.
            os.environ["VIKUNJA_API_TOKEN"] = TOKEN
            os.environ["VIKUNJA_MCP_OAUTH_ISSUER"] = ISSUER
            os.environ["VIKUNJA_MCP_OAUTH_PASSPHRASE"] = PASSPHRASE
            if public is not None:
                os.environ["INVESTMENT_PUBLIC_URL"] = public
            if admin is not None:
                os.environ["INVESTMENT_API_URL"] = admin
                os.environ["INVESTMENT_API_KEY"] = key or "k"
            return McpConfig.from_env(env_file=None)
        finally:
            for name, value in saved.items():
                os.environ.pop(name, None)
                if value is not None:
                    os.environ[name] = value

    def test_unset_is_the_disabled_state(self):
        config = self._from_env()
        self.assertIsNone(config.public_site_url)
        self.assertFalse(config.page_fetch_enabled)

    def test_set_is_enabled(self):
        config = self._from_env("http://127.0.0.1:8001")
        self.assertEqual(config.public_site_url, "http://127.0.0.1:8001")
        self.assertTrue(config.page_fetch_enabled)

    def test_a_trailing_slash_is_normalised_away(self):
        self.assertEqual(
            self._from_env("http://127.0.0.1:8001/").public_site_url,
            "http://127.0.0.1:8001",
        )

    def test_plain_http_to_a_public_host_is_allowed(self):
        """No credential is sent, so there is nothing to put in clear text."""
        self.assertEqual(
            self._from_env("http://example.com").public_site_url, "http://example.com"
        )

    def test_a_base_url_carrying_a_path_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self._from_env("http://127.0.0.1:8001/app")
        self.assertIn("no path", str(caught.exception))

    def test_something_that_is_not_a_url_is_refused(self):
        with self.assertRaises(ConfigError):
            self._from_env("127.0.0.1:8001")

    def test_pointing_it_at_the_admin_instance_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            self._from_env("http://127.0.0.1:8002", admin="http://127.0.0.1:8002")
        self.assertIn("admin", str(caught.exception))

    def test_the_public_and_admin_instances_may_both_be_configured(self):
        config = self._from_env("http://127.0.0.1:8001", admin="http://127.0.0.1:8002")
        self.assertEqual(config.public_site_url, "http://127.0.0.1:8001")
        self.assertEqual(config.investment.api_url, "http://127.0.0.1:8002")


if __name__ == "__main__":
    unittest.main()
