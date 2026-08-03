"""The tracked-content reads (task 279): what they send, and what they say.

These three tools are a **client**. Which revisions resolve, which paths are
denied, what counts as binary, what is redacted and where the limits sit are all
decided by ``api/repository_read.py`` in the investment repository, and tested
there against a real Git repository — asserting any of it a second time here
would create a second boundary that can drift from the one that actually guards
the files.

So what is asserted here is what belongs to this side:

* **The endpoint cannot be moved by an argument.** Three of these reads take
  caller-supplied values and one of them is called ``path``; the URL's path
  component is still always a constant.
* **A refusal arrives as a refusal, carrying its reason.** The application says
  why it would not read something, and that sentence is what the model needs.
  A boundary that replaced it with "the read failed" would turn a precise,
  actionable refusal into a shrug.
* **The descriptions say what the results are a view of.** Acceptance criterion
  11. A model that thinks these read the server's filesystem will reason about
  paths it cannot have and report absences that are not absences.
"""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from vikunja_claude.investment import (
    PATH_REPOSITORY_DIFF,
    PATH_REPOSITORY_FILE,
    PATH_REPOSITORY_SEARCH,
    InvestmentStatusClient,
    InvestmentStatusError,
)
from vikunja_claude.mcp import McpProtocol
from vikunja_claude.mcp_service import McpService
from vikunja_claude.vikunja import VikunjaClient

from .fakes import FakeVikunja
from .support import (
    INVESTMENT_API_KEY,
    INVESTMENT_API_URL,
    OPERATIONAL_TOOLS,
    PAYING_PAGE_TOOLS,
    REPOSITORY_CONTENT_TOOLS,
    VIKUNJA_TOOLS,
    make_investment_config,
    make_mcp_config,
)

FILE_ANSWER = {
    "commit": "b" * 40,
    "commit_short": "b" * 12,
    "path": "api/repository_read.py",
    "total_lines": 900,
    "start_line": 1,
    "end_line": 3,
    "returned_lines": 3,
    "content": "\"\"\"Tracked Git content.\"\"\"\n",
    "truncated": False,
    "redactions": 0,
    "source": "tracked git content",
}
SEARCH_ANSWER = {
    "commit": "b" * 40,
    "query": "RepositoryReadError",
    "match_count": 2,
    "file_count": 1,
    "matches": [{"path": "api/repository_read.py", "line": 118, "text": "class …"}],
    "truncated": False,
    "source": "tracked git content",
}
DIFF_ANSWER = {
    "commit": "c" * 40,
    "compared_against": "b" * 40,
    "is_merge": False,
    "file_count": 2,
    "included_file_count": 1,
    "files": [
        {"path": "api/repository_read.py", "included": True, "omitted_reason": None},
        {"path": ".env", "included": False, "omitted_reason": "sensitive_path"},
    ],
    "patch": "diff --git a/api/repository_read.py b/api/repository_read.py\n",
    "truncated": False,
    "source": "tracked git content",
}


class RecordingTransport:
    """Records the paths asked for, and answers by endpoint."""

    def __init__(self, fail: Exception | None = None):
        self.fail = fail
        self.paths: list[str] = []

    def __call__(self, path: str):
        self.paths.append(path)
        if self.fail is not None:
            raise self.fail
        endpoint = path.split("?", 1)[0]
        return {
            PATH_REPOSITORY_FILE: FILE_ANSWER,
            PATH_REPOSITORY_SEARCH: SEARCH_ANSWER,
            PATH_REPOSITORY_DIFF: DIFF_ANSWER,
        }[endpoint]

    def query(self, index: int = 0) -> dict[str, list[str]]:
        """The query arguments of the recorded request, parsed."""
        _, _, raw = self.paths[index].partition("?")
        return urllib.parse.parse_qs(raw)


class RepositoryContentTestCase(unittest.TestCase):
    transport_fail: Exception | None = None

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = make_mcp_config(
            Path(tmp.name), investment=make_investment_config()
        )
        self.recorder = RecordingTransport(fail=self.transport_fail)
        self.service = McpService(
            self.config,
            VikunjaClient(
                self.config.api_url, self.config.token, transport=FakeVikunja()
            ),
            investment=InvestmentStatusClient(
                self.config.investment.api_url,
                self.config.investment.api_key,
                transport=self.recorder,
            ),
        )
        self.protocol = McpProtocol(self.service.tools())

    def call_tool(self, name: str, **arguments) -> dict:
        response = self.protocol.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        assert response is not None
        return response

    def structured(self, name: str, **arguments) -> dict:
        response = self.call_tool(name, **arguments)
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertNotIn("isError", result, result)
        return result["structuredContent"]


# ── the surface ──────────────────────────────────────────────────────────────

class TestTheSurface(unittest.TestCase):
    def _names(self, investment) -> set[str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name), investment=investment)
        service = McpService(
            config,
            VikunjaClient(config.api_url, config.token, transport=FakeVikunja()),
        )
        return {tool.name for tool in service.tools()}

    def test_unconfigured_they_are_not_advertised(self):
        """Off means absent, not present-and-failing.

        A model reads an advertised tool as a capability. "That file does not
        exist" and "this boundary is not connected to the repository" must not
        arrive by the same route.
        """
        names = self._names(None)
        self.assertEqual(names, VIKUNJA_TOOLS)
        self.assertFalse(names & REPOSITORY_CONTENT_TOOLS)

    def test_configured_all_three_are(self):
        names = self._names(make_investment_config())
        self.assertTrue(REPOSITORY_CONTENT_TOOLS <= names)

    def test_they_ride_on_the_operational_setting_and_add_nothing_else(self):
        added = self._names(make_investment_config()) - self._names(None)
        self.assertEqual(
            added, OPERATIONAL_TOOLS | PAYING_PAGE_TOOLS | REPOSITORY_CONTENT_TOOLS
        )

    def test_the_existing_tools_are_untouched(self):
        """The ticket's last security boundary: leave the other tools alone."""
        names = self._names(make_investment_config())
        self.assertTrue(VIKUNJA_TOOLS <= names)
        self.assertTrue(OPERATIONAL_TOOLS <= names)
        self.assertTrue(PAYING_PAGE_TOOLS <= names)

    def test_an_unadvertised_tool_cannot_be_called_anyway(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name), investment=None)
        protocol = McpProtocol(
            McpService(
                config,
                VikunjaClient(config.api_url, config.token, transport=FakeVikunja()),
            ).tools()
        )
        for name in sorted(REPOSITORY_CONTENT_TOOLS):
            response = protocol.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {"path": "x", "query": "x",
                                                           "revision": "x"}},
                }
            )
            self.assertIn("error", response, name)


# ── the descriptions (acceptance criterion 11) ───────────────────────────────

class TestTheDescriptions(RepositoryContentTestCase):
    def descriptors(self) -> dict[str, dict]:
        listed = self.protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        return {t["name"]: t for t in listed["result"]["tools"]}

    def test_each_says_results_are_tracked_git_content_at_a_resolved_commit(self):
        for name in sorted(REPOSITORY_CONTENT_TOOLS):
            description = self.descriptors()[name]["description"]
            self.assertIn("tracked Git content at a resolved commit", description, name)
            self.assertIn("not from arbitrary server", description, name)

    def test_each_says_what_is_invisible_and_what_is_refused(self):
        for name in sorted(REPOSITORY_CONTENT_TOOLS):
            description = self.descriptors()[name]["description"]
            self.assertIn("Uncommitted edits", description, name)
            self.assertIn("redacted", description, name)
            self.assertIn("Read-only", description, name)

    def test_all_three_are_annotated_read_only_and_closed_world(self):
        for name in sorted(REPOSITORY_CONTENT_TOOLS):
            annotations = self.descriptors()[name]["annotations"]
            self.assertIs(annotations["readOnlyHint"], True, name)
            self.assertIs(annotations["openWorldHint"], False, name)

    def test_the_search_tool_says_it_is_not_a_regular_expression(self):
        description = self.descriptors()["search_repository_text"]["description"]
        self.assertIn("not a regular expression", description)

    def test_the_diff_tool_says_unpushed_local_commits_work(self):
        """The reason this exists rather than a GitHub connector."""
        description = self.descriptors()["read_repository_commit_diff"]["description"]
        self.assertIn("never pushed", description)

    def test_no_schema_accepts_an_undeclared_argument(self):
        for name in sorted(REPOSITORY_CONTENT_TOOLS):
            schema = self.descriptors()[name]["inputSchema"]
            self.assertIs(schema["additionalProperties"], False, name)


# ── what goes on the wire ────────────────────────────────────────────────────

class TestTheRequests(RepositoryContentTestCase):
    def test_reading_a_file_sends_its_arguments_as_query_values(self):
        answer = self.structured(
            "read_repository_file",
            path="api/repository_read.py",
            revision="b" * 40,
            start_line=1,
            end_line=3,
        )

        self.assertEqual(answer["commit"], "b" * 40)
        self.assertEqual(self.recorder.paths[0].split("?")[0], PATH_REPOSITORY_FILE)
        self.assertEqual(
            self.recorder.query(),
            {
                "path": ["api/repository_read.py"],
                "revision": ["b" * 40],
                "start_line": ["1"],
                "end_line": ["3"],
            },
        )

    def test_an_omitted_argument_is_not_sent_at_all(self):
        """Absent and empty are different questions.

        Omitting ``revision`` means "the server's default, HEAD". Sending
        ``revision=`` means "a revision named ''", which is a refusal. Passing
        the one as the other would turn every unqualified read into an error.
        """
        self.structured("read_repository_file", path="README.md")

        self.assertEqual(self.recorder.query(), {"path": ["README.md"]})

    def test_searching_sends_the_literal_and_its_filters(self):
        answer = self.structured(
            "search_repository_text",
            query="RepositoryReadError",
            path_filter="api",
            case_sensitive=False,
        )

        self.assertEqual(answer["match_count"], 2)
        self.assertEqual(self.recorder.paths[0].split("?")[0], PATH_REPOSITORY_SEARCH)
        self.assertEqual(
            self.recorder.query(),
            {
                "query": ["RepositoryReadError"],
                "path_filter": ["api"],
                "case_sensitive": ["false"],
            },
        )

    def test_a_boolean_is_sent_in_the_form_the_server_parses(self):
        """``str(False)`` is ``"False"``, which FastAPI's bool parser rejects."""
        self.structured("search_repository_text", query="x", case_sensitive=True)
        self.assertEqual(self.recorder.query()["case_sensitive"], ["true"])

        self.recorder.paths.clear()
        self.structured("search_repository_text", query="x", case_sensitive=False)
        self.assertEqual(self.recorder.query()["case_sensitive"], ["false"])

    def test_reading_a_diff_sends_the_revision(self):
        answer = self.structured("read_repository_commit_diff", revision="c" * 40)

        self.assertEqual(answer["compared_against"], "b" * 40)
        self.assertEqual(self.recorder.query(), {"revision": ["c" * 40]})

    def test_the_diff_result_keeps_the_omitted_files_it_was_given(self):
        """The client narrows nothing.

        A denied file arrives named, with its reason, and it must still be named
        after passing through here — an omitted file that this layer dropped
        would be indistinguishable from one that never changed.
        """
        answer = self.structured("read_repository_commit_diff", revision="c" * 40)

        omitted = [f for f in answer["files"] if f["omitted_reason"]]
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]["omitted_reason"], "sensitive_path")

    def test_a_missing_required_argument_is_a_protocol_error(self):
        for name, missing in (
            ("read_repository_file", "path"),
            ("search_repository_text", "query"),
            ("read_repository_commit_diff", "revision"),
        ):
            response = self.call_tool(name)
            self.assertIn("error", response, name)
            self.assertIn(missing, response["error"]["message"], name)
        self.assertEqual(self.recorder.paths, [], "nothing should have been requested")


# ── refusals ─────────────────────────────────────────────────────────────────

class TestRefusalsCarryTheirReason(RepositoryContentTestCase):
    def _http_error(self, code: int, body: bytes) -> urllib.error.HTTPError:
        import io

        return urllib.error.HTTPError(
            "http://127.0.0.1:8002/operational/repository/file",
            code,
            "refused",
            {},  # type: ignore[arg-type]
            io.BytesIO(body),
        )

    def _client_error(self, code: int, body: bytes) -> str:
        """The message an HTTP failure with that body turns into."""
        client = InvestmentStatusClient(INVESTMENT_API_URL, INVESTMENT_API_KEY)

        def fake_urlopen(request, timeout=None):
            raise self._http_error(code, body)

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(InvestmentStatusError) as caught:
                client.repository_file("whatever")
        return str(caught.exception)

    def test_a_400_repeats_the_applications_own_reason(self):
        message = self._client_error(
            400,
            b'{"detail": "\'/etc/passwd\' is an absolute path. This boundary reads '
            b'tracked content by repository-relative path only."}',
        )

        self.assertIn("absolute path", message)
        self.assertIn("repository-relative", message)

    def test_a_404_with_a_reason_is_the_file_not_the_route(self):
        """Two different 404s, and confusing them costs an afternoon.

        With a detail body the application is saying "not tracked at that
        commit". Without one, the *route* is absent — a stale admin instance
        that has not been restarted since these endpoints were merged, which is
        the diagnosis task 138 already learned to name.
        """
        with_detail = self._client_error(
            404, b'{"detail": "\'nope.py\' is not tracked at commit abc123."}'
        )
        self.assertIn("not tracked", with_detail)
        self.assertNotIn("INSTANCE_MODE", with_detail)

        without_detail = self._client_error(404, b"")
        self.assertIn("INSTANCE_MODE", without_detail)

    def test_starlettes_generic_not_found_body_is_not_treated_as_a_reason(self):
        """The absent-route 404 has a body, and it says nothing.

        A request for a route the app does not have is answered
        ``{"detail": "Not Found"}`` — verified against a live public-mode
        instance, which is where this case actually arises, because these
        endpoints are registered on the admin instance only. Taken as the
        application's own reason it replaces the diagnosis that matters for
        exactly that case with the words "Not Found", and the stale-deployment
        cause task 138 learned to name goes missing. That is a regression in the
        *existing* reads, not only the new ones, which is why it is asserted for
        both.
        """
        message = self._client_error(404, b'{"detail": "Not Found"}')

        self.assertIn("INSTANCE_MODE", message)
        self.assertNotIn("found nothing to read", message)

    def test_the_same_holds_for_the_pre_existing_reads(self):
        """Task 279 must not change what the task 138 reads say when they fail."""
        client = InvestmentStatusClient(INVESTMENT_API_URL, INVESTMENT_API_KEY)

        def fake_urlopen(request, timeout=None):
            raise self._http_error(404, b'{"detail": "Not Found"}')

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            for read in (
                client.repository_state,
                client.pipeline_status,
                client.system_health,
            ):
                with self.assertRaises(InvestmentStatusError) as caught:
                    read()
                self.assertIn("INSTANCE_MODE", str(caught.exception))

    def test_a_generic_phrase_for_another_status_is_also_not_a_reason(self):
        """Derived from the status code, not from a list containing 'Not Found'."""
        message = self._client_error(500, b'{"detail": "Internal Server Error"}')

        self.assertIn("500", message)
        self.assertNotIn("Internal Server Error:", message)

    def test_an_unparseable_body_does_not_replace_the_http_failure(self):
        message = self._client_error(400, b"<html>gateway said no</html>")

        self.assertIn("400", message)
        self.assertNotIn("gateway said no", message)

    def test_an_auth_failure_still_names_the_setting(self):
        """Unchanged by task 279: 401 is configuration, and says so."""
        message = self._client_error(401, b'{"detail": "Invalid API key"}')

        self.assertIn("INVESTMENT_API_KEY", message)

    def test_a_refusal_reaches_the_model_as_a_tool_error_not_a_transport_error(self):
        """`isError`, not a JSON-RPC error: the model is meant to read this."""

        class RefusingCase(RepositoryContentTestCase):
            transport_fail = InvestmentStatusError(
                "The investment API refused this read: '.env' is a credentials "
                "file, which this boundary never reads."
            )

        case = RefusingCase()
        case.setUp()
        response = case.call_tool("read_repository_file", path=".env")

        self.assertNotIn("error", response)
        result = response["result"]
        self.assertIs(result["isError"], True)
        self.assertIn("credentials file", result["content"][0]["text"])


# ── the client cannot be aimed, and cannot write ─────────────────────────────

class TestTheClientStaysAClient(unittest.TestCase):
    def test_the_new_reads_add_no_write_method(self):
        for verb in ("post", "put", "patch", "delete", "call", "request", "get"):
            self.assertFalse(
                hasattr(InvestmentStatusClient, verb),
                f"InvestmentStatusClient.{verb} exists",
            )

    def test_every_request_is_a_get_carrying_the_api_key(self):
        seen: dict[str, object] = {}

        class _Response:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            seen["method"] = request.get_method()
            seen["url"] = request.full_url
            seen["key"] = request.get_header("X-api-key")
            return _Response()

        client = InvestmentStatusClient(INVESTMENT_API_URL, INVESTMENT_API_KEY)
        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            client.repository_search("needle", path_filter="api")

        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["key"], INVESTMENT_API_KEY)
        self.assertTrue(
            str(seen["url"]).startswith(INVESTMENT_API_URL + PATH_REPOSITORY_SEARCH)
        )
