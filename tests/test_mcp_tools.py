"""What the two tools do, and what the boundary refuses to do.

Several tests assert on the *calls the fake Vikunja saw* rather than only on
return values. A refusal that still issued the write would satisfy a return
value check and fail these.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from vikunja_claude.html_text import html_to_text
from vikunja_claude.mcp import McpProtocol, ToolError
from vikunja_claude.mcp_service import McpService, idempotency_key
from vikunja_claude.vikunja import VikunjaClient, VikunjaError

from .fakes import PROJECT_ID, FakeVikunja
from .support import McpTestCase, make_mcp_config

OTHER_PROJECT_ID = 1

# Every Vikunja call that changes something that already exists. None of these
# may ever appear in the calls this boundary makes.
MUTATIONS = (
    (re.compile(r"^POST /tasks/\d+$"), "replace an existing task"),
    (re.compile(r"^PUT /tasks/\d+/comments$"), "comment on a task"),
    (re.compile(r"^DELETE "), "delete anything"),
    (re.compile(r"^POST /projects/\d+/views/\d+/buckets/\d+/tasks$"), "move a task"),
)


def signatures(vikunja: FakeVikunja) -> list[str]:
    return [f"{method} {path}" for method, path, _ in vikunja.calls]


class MutationFreeMixin:
    def assertTouchedNothingExisting(self) -> None:
        for signature in signatures(self.vikunja):
            for pattern, what in MUTATIONS:
                if pattern.match(signature):
                    self.fail(f"the boundary issued {signature!r}, which would {what}")


class TestGetTask(MutationFreeMixin, McpTestCase):
    comments = {
        9: [
            {
                "id": 3,
                "comment": "<p>Blocked on the <code>URTH</code> backfill.</p>",
                "created": "2026-07-27T09:00:00Z",
                "author": {"username": "glen"},
            }
        ]
    }

    def test_it_returns_the_whole_ticket(self):
        task = self.service.get_task(9)
        self.assertEqual(task["task_id"], 9)
        self.assertEqual(task["ticket"], 33)
        self.assertEqual(task["title"], "#33 Back up Vikunja database")
        self.assertEqual(task["bucket"], "Ready")
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["labels"], ["Operations"])
        self.assertEqual(task["created"], "2026-07-26T05:05:50Z")
        self.assertEqual(task["url"], "http://127.0.0.1:3456/tasks/9")

    def test_the_description_arrives_as_readable_text_not_markup(self):
        task = self.service.get_task(9)
        self.assertIn("authoritative", task["description"])
        self.assertNotIn("<strong>", task["description"])

    def test_it_returns_comments(self):
        comments = self.service.get_task(9)["comments"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "glen")
        self.assertEqual(comments[0]["created"], "2026-07-27T09:00:00Z")
        self.assertEqual(comments[0]["text"], "Blocked on the `URTH` backfill.")

    def test_a_done_ticket_reports_done(self):
        self.assertEqual(self.service.get_task(1)["status"], "done")

    def test_an_unknown_task_is_refused_with_a_reason(self):
        with self.assertRaises(ToolError) as caught:
            self.service.get_task(4242)
        self.assertIn("4242", str(caught.exception))

    def test_reading_changes_nothing(self):
        self.service.get_task(9)
        self.assertTouchedNothingExisting()


class TestCreateTask(MutationFreeMixin, McpTestCase):
    TITLE = "Pin the S6 run in the S8 context"
    BODY = "The deterministic layer is recorded, not pinned.\n\nSee task 158."

    def create(self, **overrides):
        arguments = {
            "project_id": PROJECT_ID,
            "title": self.TITLE,
            "description": self.BODY,
        }
        arguments.update(overrides)
        return self.service.create_task(**arguments)

    def stored(self, task_id: int) -> dict:
        for tasks in self.vikunja.layout.values():
            for item in tasks:
                if item["id"] == task_id:
                    return item
        self.fail(f"task {task_id} is not on the board")

    def test_it_creates_the_task_and_returns_its_identity(self):
        result = self.create()
        self.assertTrue(result["created"])
        self.assertEqual(result["project_id"], PROJECT_ID)
        self.assertEqual(result["url"], f"http://127.0.0.1:3456/tasks/{result['task_id']}")
        self.assertEqual(self.stored(result["task_id"])["title"], self.TITLE)

    def test_the_stored_description_is_the_one_that_was_asked_for(self):
        result = self.create()
        stored = self.stored(result["task_id"])["description"]
        self.assertEqual(html_to_text(stored), self.BODY)

    def test_the_description_is_escaped_never_interpreted(self):
        result = self.create(description="Watch out for <script>alert(1)</script>")
        stored = self.stored(result["task_id"])["description"]
        self.assertNotIn("<script>", stored)
        self.assertIn("&lt;script&gt;", stored)

    def test_creating_touches_no_existing_task(self):
        self.create()
        self.assertTouchedNothingExisting()

    def test_the_creation_is_recorded(self):
        result = self.create()
        lines = self.config.ledger_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["task_id"], result["task_id"])
        self.assertEqual(record["title"], self.TITLE)
        self.assertEqual(record["project_id"], PROJECT_ID)
        self.assertTrue(record["created_at"])


class TestCreateTaskRefusals(MutationFreeMixin, McpTestCase):
    TITLE = "A ticket"
    BODY = "A body."

    def board_size(self) -> int:
        return sum(len(tasks) for tasks in self.vikunja.layout.values())

    def assertNothingCreated(self, before: int) -> None:
        self.assertEqual(self.board_size(), before)
        self.assertNotIn(
            f"PUT /projects/{PROJECT_ID}/tasks", signatures(self.vikunja)
        )

    def test_another_project_is_refused_not_redirected(self):
        before = self.board_size()
        with self.assertRaises(ToolError) as caught:
            self.service.create_task(OTHER_PROJECT_ID, self.TITLE, self.BODY)
        message = str(caught.exception)
        self.assertIn(str(OTHER_PROJECT_ID), message)
        self.assertIn("AI Alpha Engine", message)
        self.assertIn("Nothing was created", message)
        self.assertNothingCreated(before)

    def test_a_blank_title_is_refused(self):
        before = self.board_size()
        with self.assertRaises(ToolError):
            self.service.create_task(PROJECT_ID, "   ", self.BODY)
        self.assertNothingCreated(before)

    def test_a_blank_description_is_refused(self):
        before = self.board_size()
        with self.assertRaises(ToolError):
            self.service.create_task(PROJECT_ID, self.TITLE, "")
        self.assertNothingCreated(before)

    def test_an_oversized_title_is_refused(self):
        before = self.board_size()
        with self.assertRaises(ToolError):
            self.service.create_task(PROJECT_ID, "x" * 5000, self.BODY)
        self.assertNothingCreated(before)

    def test_an_oversized_description_is_refused(self):
        before = self.board_size()
        with self.assertRaises(ToolError):
            self.service.create_task(PROJECT_ID, self.TITLE, "x" * 100000)
        self.assertNothingCreated(before)


class TestUnresolvableProject(McpTestCase):
    """Vikunja is unreachable: the boundary fails, it does not improvise."""

    vikunja_fail = VikunjaError("connection refused")

    def test_the_read_fails_explicitly(self):
        with self.assertRaises(ToolError) as caught:
            self.service.get_task(9)
        self.assertIn("AI Alpha Engine", str(caught.exception))

    def test_the_create_fails_explicitly_and_creates_nothing(self):
        with self.assertRaises(ToolError) as caught:
            self.service.create_task(PROJECT_ID, "A ticket", "A body.")
        self.assertIn("Cannot resolve the project", str(caught.exception))
        self.assertFalse(self.config.ledger_path.exists())


class TestRetriesDoNotDuplicate(McpTestCase):
    TITLE = "Pin the S6 run"
    BODY = "The deterministic layer is recorded, not pinned."

    def create(self, service=None):
        return (service or self.service).create_task(PROJECT_ID, self.TITLE, self.BODY)

    def board_size(self) -> int:
        return sum(len(tasks) for tasks in self.vikunja.layout.values())

    def test_an_identical_retry_returns_the_first_task(self):
        first = self.create()
        before = self.board_size()

        second = self.create()

        self.assertFalse(second["created"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(self.board_size(), before)

    def test_a_retry_after_a_restart_still_does_not_duplicate(self):
        """The ledger is on disk, so restarting does not reopen the door."""
        first = self.create()

        restarted = McpService(self.config, self.client)
        second = self.create(service=restarted)

        self.assertFalse(second["created"])
        self.assertEqual(second["task_id"], first["task_id"])

    def test_a_genuinely_different_ticket_is_created(self):
        first = self.create()
        second = self.service.create_task(PROJECT_ID, self.TITLE, "A different body.")
        self.assertTrue(second["created"])
        self.assertNotEqual(second["task_id"], first["task_id"])

    def test_whitespace_around_the_same_content_is_the_same_request(self):
        self.assertEqual(
            idempotency_key(PROJECT_ID, self.TITLE, self.BODY),
            idempotency_key(PROJECT_ID, f"  {self.TITLE}  ", f"\n{self.BODY}\n"),
        )

    def test_the_same_content_in_another_project_is_a_different_request(self):
        self.assertNotEqual(
            idempotency_key(PROJECT_ID, self.TITLE, self.BODY),
            idempotency_key(OTHER_PROJECT_ID, self.TITLE, self.BODY),
        )

    def test_a_corrupt_ledger_line_does_not_block_creation(self):
        self.config.ledger_path.write_text("{not json\n", encoding="utf-8")
        self.assertTrue(self.create()["created"])


class TestThroughTheProtocol(McpTestCase):
    """The same guarantees, driven the way a client drives them."""

    def test_get_task_comes_back_as_a_tool_result(self):
        response = self.call_tool("get_task", task_id=9)
        self.assertEqual(response["result"]["structuredContent"]["task_id"], 9)

    def test_a_refused_create_is_a_visible_error_not_a_silent_success(self):
        response = self.call_tool(
            "create_task",
            project_id=OTHER_PROJECT_ID,
            title="A ticket",
            description="A body.",
        )
        self.assertTrue(response["result"]["isError"])
        self.assertIn("Nothing was created", response["result"]["content"][0]["text"])

    def test_a_create_comes_back_with_the_authoritative_id_and_url(self):
        response = self.call_tool(
            "create_task",
            project_id=PROJECT_ID,
            title="A ticket",
            description="A body.",
        )
        created = response["result"]["structuredContent"]
        self.assertTrue(created["created"])
        self.assertEqual(created["url"], f"http://127.0.0.1:3456/tasks/{created['task_id']}")


class TestProjectOverrideIsHonoured(unittest.TestCase):
    """A configured project id is authoritative and is not looked up by title."""

    def test_the_override_is_the_only_project_that_may_be_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_mcp_config(Path(tmp), project_id=PROJECT_ID)
            vikunja = FakeVikunja()
            client = VikunjaClient(config.api_url, config.token, transport=vikunja)
            service = McpService(config, client)

            self.assertEqual(service.allowed_project_id(), PROJECT_ID)
            self.assertNotIn("GET /projects", signatures(vikunja))


class TestTheSurfaceIsTwoTools(McpTestCase):
    def test_the_service_offers_exactly_the_advertised_operations(self):
        self.assertEqual(
            {tool.name for tool in self.service.tools()}, {"get_task", "create_task"}
        )

    def test_driving_every_tool_never_touches_an_existing_task(self):
        """Exercise the whole surface, then check nothing existing moved."""
        protocol = McpProtocol(self.service.tools())
        self.call_tool("get_task", task_id=9)
        self.call_tool(
            "create_task", project_id=PROJECT_ID, title="A ticket", description="A body."
        )

        self.assertEqual(protocol.tool_names, {"get_task", "create_task"})
        for signature in signatures(self.vikunja):
            for pattern, what in MUTATIONS:
                self.assertIsNone(
                    pattern.match(signature),
                    f"the boundary issued {signature!r}, which would {what}",
                )


if __name__ == "__main__":
    unittest.main()
