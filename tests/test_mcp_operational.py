"""The operational reads (task 138): what they expose, and what they refuse.

Three properties are asserted here that no other test in the tree covers.

* **Off means absent.** Unconfigured, the three tools are not advertised at all.
  A model reads an advertised tool as a capability and reports its failure as a
  fact about the system; "system health unavailable" and "system health not
  configured" must not arrive by the same route.
* **The client cannot be aimed.** There is no path argument anywhere between the
  tool and the HTTP request, so this boundary cannot reach any endpoint of the
  investment API other than the three named reads.
* **A failed read is never a healthy answer.** Every failure path is a `ToolError`
  that says nothing was read.
"""

from __future__ import annotations

import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from vikunja_claude.config import ConfigError, InvestmentConfig
from vikunja_claude.investment import (
    PATH_PIPELINE,
    PATH_REPOSITORY,
    PATH_SYSTEM_HEALTH,
    READ_PATHS,
    InvestmentStatusClient,
    InvestmentStatusError,
)
from vikunja_claude.mcp import McpProtocol
from vikunja_claude.mcp_service import McpService
from vikunja_claude.vikunja import VikunjaClient

from .fakes import FakeVikunja
from .support import (
    INVESTMENT_API_KEY,
    INVESTMENT_TOOLS,
    OPERATIONAL_TOOLS,
    VIKUNJA_TOOLS,
    make_investment_config,
    make_mcp_config,
)

REPOSITORY_ANSWER = {
    "readable": True,
    "repository": "investment",
    "branch": "main",
    "detached_head": False,
    "commit": "a" * 40,
    "commit_short": "a" * 12,
    "commit_subject": "Merge task 138",
    "committed_at": "2026-07-30T11:00:04Z",
    "clean": True,
    "changed_files": 0,
    "untracked_entries": 0,
}
PIPELINE_ANSWER = {"run_present": True, "status": "succeeded", "stage_count": 25}
HEALTH_ANSWER = {"overall": "ok", "checks": {}}

ANSWERS = {
    PATH_REPOSITORY: REPOSITORY_ANSWER,
    PATH_PIPELINE: PIPELINE_ANSWER,
    PATH_SYSTEM_HEALTH: HEALTH_ANSWER,
}


class RecordingInvestment:
    """A stand-in for the investment API that records the paths it was asked for."""

    def __init__(self, answers=None, fail: Exception | None = None):
        self.answers = answers if answers is not None else ANSWERS
        self.fail = fail
        self.paths: list[str] = []

    def __call__(self, path: str):
        self.paths.append(path)
        if self.fail is not None:
            raise self.fail
        return self.answers[path]


class OperationalTestCase(unittest.TestCase):
    """A service with the operational reads switched on, wired to a recorder."""

    investment_fail: Exception | None = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.config = make_mcp_config(
            self.state_dir, investment=make_investment_config()
        )
        self.recorder = RecordingInvestment(fail=self.investment_fail)
        self.vikunja = FakeVikunja()
        self.service = McpService(
            self.config,
            VikunjaClient(self.config.api_url, self.config.token, transport=self.vikunja),
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

    def test_unconfigured_the_operational_tools_are_not_advertised(self):
        service = self._service(None)
        names = {tool.name for tool in service.tools()}
        self.assertEqual(names, VIKUNJA_TOOLS)
        self.assertFalse(names & OPERATIONAL_TOOLS)
        self.assertFalse(service.operational_reads_enabled)

    def test_configured_they_are(self):
        service = self._service(make_investment_config())
        names = {tool.name for tool in service.tools()}
        # The same two settings also switch on the authenticated page read
        # (task 239) — same key, same admin instance. The set is still exact, so
        # a tool nobody meant to add still fails here.
        self.assertEqual(names, VIKUNJA_TOOLS | INVESTMENT_TOOLS)
        self.assertTrue(service.operational_reads_enabled)

    def test_disabling_them_leaves_the_vikunja_surface_untouched(self):
        """Task 138: "can be disabled without affecting Vikunja"."""
        off = {tool.name for tool in self._service(None).tools()}
        on = {tool.name for tool in self._service(make_investment_config()).tools()}
        self.assertEqual(on - off, INVESTMENT_TOOLS)
        self.assertEqual(off - on, set())

    def test_an_unadvertised_tool_cannot_be_called_anyway(self):
        """Absent from `tools/list` and unreachable through `tools/call`."""
        protocol = McpProtocol(self._service(None).tools())
        for name in sorted(OPERATIONAL_TOOLS):
            response = protocol.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                }
            )
            self.assertIn("error", response, name)

    def test_a_service_built_without_a_client_but_with_config_still_reads(self):
        """The client is built from configuration, not only injected in tests."""
        service = self._service(make_investment_config())
        self.assertIsNotNone(service.investment)
        self.assertEqual(
            service.investment.api_url, make_investment_config().api_url
        )


# ── the three reads ──────────────────────────────────────────────────────────

class TestTheReads(OperationalTestCase):
    def structured(self, name):
        response = self.call_tool(name)
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertNotIn("isError", result, result)
        return result["structuredContent"]

    def test_repository_state_comes_back_whole(self):
        answer = self.structured("get_repository_state")
        self.assertEqual(answer["commit"], "a" * 40)
        self.assertIs(answer["clean"], True)
        self.assertEqual(self.recorder.paths, [PATH_REPOSITORY])

    def test_pipeline_status_comes_back_whole(self):
        answer = self.structured("get_pipeline_status")
        self.assertEqual(answer["status"], "succeeded")
        self.assertEqual(self.recorder.paths, [PATH_PIPELINE])

    def test_system_health_comes_back_whole(self):
        answer = self.structured("get_system_health")
        self.assertEqual(answer["overall"], "ok")
        self.assertEqual(self.recorder.paths, [PATH_SYSTEM_HEALTH])

    def test_none_of_them_take_an_argument(self):
        for name in sorted(OPERATIONAL_TOOLS):
            tool = next(t for t in self.service.tools() if t.name == name)
            self.assertEqual(tool.input_schema["properties"], {}, name)
            self.assertEqual(tool.required_arguments(), [], name)
            self.assertIs(tool.input_schema["additionalProperties"], False, name)

    def test_all_three_are_marked_read_only(self):
        for name in sorted(OPERATIONAL_TOOLS):
            tool = next(t for t in self.service.tools() if t.name == name)
            self.assertIs(tool.annotations["readOnlyHint"], True, name)

    def test_reading_the_server_state_touches_no_task(self):
        for name in sorted(OPERATIONAL_TOOLS):
            self.call_tool(name)
        self.assertEqual(self.vikunja.calls, [])


# ── the client cannot be aimed ───────────────────────────────────────────────

class TestTheClientSurface(unittest.TestCase):
    def test_there_are_exactly_three_read_paths(self):
        self.assertEqual(
            set(READ_PATHS), {PATH_REPOSITORY, PATH_PIPELINE, PATH_SYSTEM_HEALTH}
        )

    def test_the_client_exposes_no_way_to_name_a_path(self):
        """No `get(path)`, so no endpoint of the investment API but these three."""
        public = {
            name
            for name in dir(InvestmentStatusClient)
            if not name.startswith("_")
        }
        self.assertEqual(
            public, {"repository_state", "pipeline_status", "system_health"}
        )

    def test_the_client_has_no_write_method(self):
        """Absent, not refused."""
        for verb in ("post", "put", "patch", "delete", "call", "request"):
            self.assertFalse(
                hasattr(InvestmentStatusClient, verb),
                f"InvestmentStatusClient.{verb} exists",
            )

    def test_every_request_is_a_get_carrying_the_api_key(self):
        seen = {}

        class _Response:
            status = 200

            def read(self):
                return b'{"overall": "ok"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=None):
            seen["method"] = request.get_method()
            seen["url"] = request.full_url
            seen["key"] = request.get_header("X-api-key")
            return _Response()

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            client = InvestmentStatusClient("http://127.0.0.1:8002", INVESTMENT_API_KEY)
            client.system_health()

        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["url"], "http://127.0.0.1:8002" + PATH_SYSTEM_HEALTH)
        self.assertEqual(seen["key"], INVESTMENT_API_KEY)

    def test_a_non_object_answer_is_refused(self):
        client = InvestmentStatusClient(
            "http://127.0.0.1:8002", "k", transport=lambda path: ["not", "an", "object"]
        )
        with self.assertRaises(InvestmentStatusError):
            client.system_health()


# ── a failed read is never a healthy answer ──────────────────────────────────

class TestFailuresAreNotAnswers(OperationalTestCase):
    def error_text(self, name) -> str:
        response = self.call_tool(name)
        result = response["result"]
        self.assertTrue(result.get("isError"), result)
        return result["content"][0]["text"]

    def test_an_unreachable_api_says_nothing_was_read(self):
        self.recorder.fail = InvestmentStatusError(
            "Cannot reach the investment API at http://127.0.0.1:8002: refused. "
            "The operational read was not performed; nothing is known about the "
            "state it would have reported."
        )
        text = self.error_text("get_system_health")
        self.assertIn("not performed", text)
        self.assertNotIn('"overall"', text)

    def test_a_rejected_key_is_reported_as_configuration_not_as_health(self):
        client = InvestmentStatusClient("http://127.0.0.1:8002", "wrong")
        message = client._explain_status(403, PATH_SYSTEM_HEALTH)
        self.assertIn("INVESTMENT_API_KEY", message)
        self.assertIn("not a statement about the system's health", message)

    def test_a_404_names_both_of_its_causes(self):
        """The wrong instance, *and* the right instance running older code.

        Naming only the first is how a stale deployment gets misdiagnosed as a
        misconfiguration — which is what happened the first time these reads were
        pointed at a live admin instance that predated them.
        """
        client = InvestmentStatusClient("http://127.0.0.1:8001", "k")
        message = client._explain_status(404, PATH_PIPELINE)
        self.assertIn("INVESTMENT_API_URL", message)
        self.assertIn("admin", message)
        self.assertIn("restarted", message)
        self.assertIn("Nothing was read", message)

    def test_the_api_error_body_is_not_repeated_to_the_model(self):
        """It is the other application's error text and may name internals."""
        body = b"Traceback: /home/glen/stacks/investment/api/secrets.py line 3"

        class _HttpError(urllib.error.HTTPError):
            def __init__(self):
                super().__init__(
                    "http://127.0.0.1:8002" + PATH_PIPELINE, 500, "boom", {}, None
                )

            def read(self):
                return body

        def fake_urlopen(request, timeout=None):
            raise _HttpError()

        with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
            client = InvestmentStatusClient("http://127.0.0.1:8002", "k")
            with self.assertRaises(InvestmentStatusError) as caught:
                client.pipeline_status()

        self.assertNotIn("secrets.py", str(caught.exception))
        self.assertIn("500", str(caught.exception))


class TestTheUnconfiguredCallRefusesLoudly(unittest.TestCase):
    def test_calling_a_read_with_no_configuration_names_the_settings(self):
        """Reached only if a tool was called that should not have been advertised."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name), investment=None)
        service = McpService(
            config,
            VikunjaClient(config.api_url, config.token, transport=FakeVikunja()),
        )
        from vikunja_claude.mcp import ToolError

        for read in (
            service.repository_state,
            service.pipeline_status,
            service.system_health,
        ):
            with self.assertRaises(ToolError) as caught:
                read()
            self.assertIn("INVESTMENT_API_URL", str(caught.exception))
            self.assertIn("nothing is known", str(caught.exception))


# ── configuration ────────────────────────────────────────────────────────────

class TestInvestmentConfig(unittest.TestCase):
    def _from_env(self, url=None, key=None):
        import os

        saved = {
            name: os.environ.pop(name, None)
            for name in ("INVESTMENT_API_URL", "INVESTMENT_API_KEY")
        }
        try:
            if url is not None:
                os.environ["INVESTMENT_API_URL"] = url
            if key is not None:
                os.environ["INVESTMENT_API_KEY"] = key
            return InvestmentConfig.from_env()
        finally:
            for name, value in saved.items():
                os.environ.pop(name, None)
                if value is not None:
                    os.environ[name] = value

    def test_neither_set_is_the_disabled_state(self):
        self.assertIsNone(self._from_env())

    def test_both_set_is_enabled(self):
        config = self._from_env("http://127.0.0.1:8002", "k")
        self.assertEqual(config.api_url, "http://127.0.0.1:8002")
        self.assertEqual(config.api_key, "k")

    def test_half_configured_is_a_startup_failure_not_a_broken_tool(self):
        with self.assertRaises(ConfigError) as caught:
            self._from_env("http://127.0.0.1:8002", None)
        self.assertIn("INVESTMENT_API_KEY", str(caught.exception))

        with self.assertRaises(ConfigError) as caught:
            self._from_env(None, "k")
        self.assertIn("INVESTMENT_API_URL", str(caught.exception))

    def test_a_trailing_slash_is_normalised_away(self):
        self.assertEqual(
            self._from_env("http://127.0.0.1:8002/", "k").api_url,
            "http://127.0.0.1:8002",
        )

    def test_plain_http_to_a_tailnet_address_is_allowed(self):
        """The admin instance is bound to one and serves http; the tailnet is the
        encryption."""
        for host in ("100.105.117.8:8002", "aiserver.tail36601d.ts.net:8002"):
            self.assertIsNotNone(self._from_env(f"http://{host}", "k"))

    def test_plain_http_to_a_public_host_is_refused(self):
        """That would put the API key on the wire in clear text."""
        with self.assertRaises(ConfigError) as caught:
            self._from_env("http://example.com", "k")
        self.assertIn("clear text", str(caught.exception))

    def test_https_anywhere_is_allowed(self):
        self.assertIsNotNone(self._from_env("https://example.com", "k"))

    def test_a_base_url_carrying_a_path_is_refused(self):
        """The read paths are appended, so a base with a path builds the wrong URL."""
        with self.assertRaises(ConfigError) as caught:
            self._from_env("http://127.0.0.1:8002/api/v1", "k")
        self.assertIn("no path", str(caught.exception))

    def test_something_that_is_not_a_url_is_refused(self):
        with self.assertRaises(ConfigError):
            self._from_env("127.0.0.1:8002", "k")


if __name__ == "__main__":
    unittest.main()
