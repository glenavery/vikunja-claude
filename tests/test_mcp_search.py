"""`search_tasks`: the approved search, and what it deliberately cannot do.

Task 138 approved "search tasks by text, project and status". Two of those three
are parameters here; the project is not, and that omission is asserted rather
than assumed — this boundary is configured with one board, and a search that
could name another would be a wider read than the one approved.
"""

from __future__ import annotations

import unittest
import urllib.parse

from vikunja_claude.mcp_service import McpService, SEARCH_STATUSES
from vikunja_claude.vikunja import OPEN_TASKS_FILTER, VikunjaClient

from .fakes import PROJECT_ID, FakeVikunja, task
from .support import McpTestCase

LAYOUT = {
    "Backlog": [
        task(5, "#29 Admin: surface generation mode", "2026-07-26T05:01:00Z"),
        task(
            6,
            "#30 Cloudflare client-IP hop",
            "2026-07-26T05:02:00Z",
            "<p>The hop is trusted without verification.</p>",
            priority=4,
        ),
    ],
    "Ready": [
        task(
            9,
            "#33 Back up Vikunja database",
            "2026-07-26T05:05:50Z",
            "<p>Vikunja is the authoritative queue.</p>",
            labels=["Operations"],
        ),
    ],
    "In Progress": [],
    "Waiting": [
        task(
            11,
            "#35 Connect ChatGPT to AI Server",
            "2026-07-26T05:20:00Z",
            "<p>Read authoritative information and create tickets.</p>",
        ),
    ],
    "Done": [
        task(
            1,
            "#25 Back up the reports step",
            "2026-07-26T04:59:00Z",
            "<p>Already handled last week.</p>",
            done=True,
        ),
    ],
}


class SearchTestCase(McpTestCase):
    layout = LAYOUT

    def search(self, **arguments):
        response = self.call_tool("search_tasks", **arguments)
        self.assertNotIn("error", response, response)
        result = response["result"]
        self.assertNotIn("isError", result, result)
        return result["structuredContent"]


class TestWhatItFinds(SearchTestCase):
    def test_a_title_substring_matches(self):
        found = self.search(text="Cloudflare")
        self.assertEqual([t["task_id"] for t in found["tasks"]], [6])
        self.assertEqual(found["tasks"][0]["matched_in"], "title")

    def test_matching_is_case_insensitive(self):
        self.assertEqual(self.search(text="cloudflare")["count"], 1)
        self.assertEqual(self.search(text="CLOUDFLARE")["count"], 1)

    def test_a_description_substring_matches_and_says_so(self):
        found = self.search(text="authoritative")
        by_id = {t["task_id"]: t for t in found["tasks"]}
        self.assertIn(9, by_id)
        self.assertIn(11, by_id)
        self.assertEqual(by_id[9]["matched_in"], "description")

    def test_the_description_is_searched_as_text_not_as_html(self):
        """A tag name is not content: searching for `p` must not match every task."""
        found = self.search(text="<p>")
        self.assertEqual(found["count"], 0)

    def test_title_matches_are_ranked_above_description_matches(self):
        found = self.search(text="Back up", status="any")
        self.assertEqual([t["matched_in"] for t in found["tasks"]][:2],
                         ["title", "title"])

    def test_no_match_is_an_empty_answer_not_an_error(self):
        found = self.search(text="quantum tunnelling")
        self.assertEqual(found["count"], 0)
        self.assertEqual(found["tasks"], [])
        # How much was looked at, so "nothing found" is distinguishable from
        # "nothing was searched".
        self.assertGreater(found["searched"], 0)

    def test_the_answer_names_the_board_it_came_from(self):
        found = self.search(text="Vikunja")
        self.assertEqual(found["project"], "AI Alpha Engine")
        self.assertEqual(found["project_id"], PROJECT_ID)


class TestStatus(SearchTestCase):
    def test_open_is_the_default_and_excludes_done_tasks(self):
        found = self.search(text="Back up")
        self.assertEqual(found["status"], "open")
        self.assertEqual([t["task_id"] for t in found["tasks"]], [9])

    def test_done_finds_only_finished_tasks(self):
        found = self.search(text="Back up", status="done")
        self.assertEqual([t["task_id"] for t in found["tasks"]], [1])
        self.assertEqual(found["tasks"][0]["status"], "done")

    def test_any_finds_both(self):
        found = self.search(text="Back up", status="any")
        self.assertEqual({t["task_id"] for t in found["tasks"]}, {1, 9})

    def test_a_status_that_is_not_a_status_is_refused(self):
        """Not silently narrowed: "closed" is not "done", and guessing would lie."""
        response = self.call_tool("search_tasks", text="Back up", status="closed")
        self.assertTrue(response["result"].get("isError"))
        self.assertIn("open", response["result"]["content"][0]["text"])

    def test_the_schema_advertises_exactly_the_accepted_statuses(self):
        tool = next(t for t in self.service.tools() if t.name == "search_tasks")
        self.assertEqual(
            tool.input_schema["properties"]["status"]["enum"], list(SEARCH_STATUSES)
        )


class TestWhatItRefuses(SearchTestCase):
    def test_blank_text_is_refused_rather_than_answered_with_the_board(self):
        response = self.call_tool("search_tasks", text="   ")
        self.assertTrue(response["result"].get("isError"))
        self.assertIn("list_open_tasks", response["result"]["content"][0]["text"])

    def test_missing_text_is_a_protocol_error(self):
        response = self.call_tool("search_tasks")
        self.assertIn("error", response)
        self.assertIn("text", response["error"]["message"])

    def test_the_project_cannot_be_named_by_the_caller(self):
        """One configured board. A project argument would be a wider read."""
        tool = next(t for t in self.service.tools() if t.name == "search_tasks")
        self.assertNotIn("project_id", tool.input_schema["properties"])
        self.assertNotIn("project", tool.input_schema["properties"])
        self.assertIs(tool.input_schema["additionalProperties"], False)

    def test_the_text_never_reaches_vikunjas_filter_language(self):
        """Matching happens in Python, so the query can only be a substring.

        The only filter this boundary ever sends is the constant
        ``OPEN_TASKS_FILTER``. A model-supplied fragment interpolated into a
        filter expression is the shape this test exists to keep out, so the
        search text here is written to be an expression if anything treated it
        as one.
        """
        self.search(text="done = true || id > 0")

        sent = set()
        for _, path, _ in self.vikunja.calls:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            sent.update(query.get("filter", []))

        self.assertLessEqual(sent, {OPEN_TASKS_FILTER})

    def test_searching_mutates_nothing(self):
        self.search(text="Vikunja", status="any")
        for method, path, _ in self.vikunja.calls:
            self.assertEqual(method, "GET", f"search issued {method} {path}")


class TestItReadsTheWholeBoard(unittest.TestCase):
    """A search that stopped at a page would answer "no" from an unread board."""

    def test_every_bucket_page_is_walked(self):
        many = {
            "Backlog": [
                task(100 + n, f"#{100 + n} filler {n}", "2026-07-26T05:00:00Z")
                for n in range(120)
            ] + [task(999, "#999 the needle", "2026-07-26T05:00:00Z")],
            "Ready": [],
            "In Progress": [],
            "Waiting": [],
            "Done": [],
        }
        import tempfile
        from pathlib import Path

        from .support import make_mcp_config

        with tempfile.TemporaryDirectory() as tmp:
            config = make_mcp_config(Path(tmp))
            vikunja = FakeVikunja(layout=many)
            service = McpService(
                config, VikunjaClient(config.api_url, config.token, transport=vikunja)
            )
            found = service.search_tasks("the needle")

        self.assertEqual([t["task_id"] for t in found["tasks"]], [999])
        self.assertEqual(found["searched"], 121)


if __name__ == "__main__":
    unittest.main()
