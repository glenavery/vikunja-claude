"""What the tools do, and what the boundary refuses to do.

Several tests assert on the *calls the fake Vikunja saw* rather than only on
return values. A refusal that still issued the write would satisfy a return
value check and fail these.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from vikunja_claude.html_text import html_to_text
from vikunja_claude.mcp import McpProtocol, ToolError
from vikunja_claude.mcp_service import McpService, idempotency_key
from vikunja_claude.vikunja import VikunjaClient, VikunjaError

from .fakes import (
    PROJECT_ID,
    TRADER_PROJECT_ID,
    VIEW_ID,
    FakeVikunja,
    FilterIgnoringVikunja,
    task,
)
from .support import (
    READ_TOOLS,
    VIKUNJA_TOOLS,
    WRITE_TOOLS,
    McpTestCase,
    make_mcp_config,
)

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
        task = self.service.get_task(8)
        self.assertEqual(task["task_number"], 8)
        self.assertEqual(task["reference"], "#8")
        self.assertEqual(task["vikunja_task_id"], 9)
        self.assertEqual(task["title"], "#33 Back up Vikunja database")
        self.assertEqual(task["bucket"], "Ready")
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["labels"], ["Operations"])
        self.assertEqual(task["created"], "2026-07-26T05:05:50Z")
        self.assertEqual(task["url"], "http://127.0.0.1:3456/tasks/9")

    def test_the_description_arrives_as_readable_text_not_markup(self):
        task = self.service.get_task(8)
        self.assertIn("authoritative", task["description"])
        self.assertNotIn("<strong>", task["description"])

    def test_it_returns_comments(self):
        comments = self.service.get_task(8)["comments"]
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "glen")
        self.assertEqual(comments[0]["created"], "2026-07-27T09:00:00Z")
        self.assertEqual(comments[0]["text"], "Blocked on the `URTH` backfill.")

    def test_a_done_ticket_reports_done(self):
        self.assertEqual(self.service.get_task(2)["status"], "done")

    def test_an_unknown_task_is_refused_with_a_reason(self):
        with self.assertRaises(ToolError) as caught:
            self.service.get_task(4242)
        self.assertIn("4242", str(caught.exception))

    def test_reading_changes_nothing(self):
        self.service.get_task(8)
        self.assertTouchedNothingExisting()


class TestListOpenTasks(MutationFreeMixin, McpTestCase):
    """The default board: four open tasks across three columns, one done."""

    OPEN = {5, 9, 10, 11}

    def ids(self, **filters) -> set[int]:
        return {t["vikunja_task_id"] for t in self.service.list_open_tasks(**filters)["tasks"]}

    def test_it_returns_every_open_task_on_the_board(self):
        self.assertEqual(self.ids(), self.OPEN)

    def test_a_done_task_is_excluded_although_it_is_on_the_board(self):
        """#2 (task id 1) is in Done. It is readable and must not be listed."""
        self.assertEqual(self.service.get_task(2)["status"], "done")
        self.assertNotIn(1, self.ids())

    def test_the_count_is_the_number_of_tasks_returned(self):
        listed = self.service.list_open_tasks()
        self.assertEqual(listed["count"], len(listed["tasks"]))
        self.assertEqual(listed["count"], len(self.OPEN))

    def test_every_task_carries_the_fields_a_board_view_needs(self):
        found = {t["vikunja_task_id"]: t for t in self.service.list_open_tasks()["tasks"]}[9]
        self.assertEqual(found["title"], "#33 Back up Vikunja database")
        self.assertEqual(found["task_number"], 8)
        self.assertEqual(found["reference"], "#8")
        self.assertEqual(found["status"], "open")
        self.assertEqual(found["bucket"], "Ready")
        self.assertEqual(found["labels"], ["Operations"])
        self.assertEqual(found["priority"], 0)
        self.assertEqual(found["priority_label"], "unset")
        self.assertEqual(found["created"], "2026-07-26T05:05:50Z")
        self.assertEqual(found["updated"], "2026-07-26T05:05:50Z")
        self.assertEqual(found["url"], "http://127.0.0.1:3456/tasks/9")

    def test_it_names_the_project_it_listed(self):
        listed = self.service.list_open_tasks()
        self.assertEqual(listed["project"], "AI Alpha Engine")
        self.assertEqual(listed["project_id"], PROJECT_ID)

    def test_it_does_not_carry_descriptions(self):
        """A board listing is not 20 full ticket bodies; get_task is for that."""
        for entry in self.service.list_open_tasks()["tasks"]:
            self.assertNotIn("description", entry)
            self.assertNotIn("comments", entry)

    def test_listing_changes_nothing(self):
        self.service.list_open_tasks()
        self.assertTouchedNothingExisting()

    def test_it_comes_back_through_the_protocol(self):
        listed = self.call_tool("list_open_tasks")["result"]["structuredContent"]
        self.assertEqual({t["vikunja_task_id"] for t in listed["tasks"]}, self.OPEN)

    def test_it_is_callable_with_no_arguments_at_all(self):
        """"read the open tickets" carries no bucket and no label."""
        response = self.call_tool("list_open_tasks")
        self.assertNotIn("isError", response["result"])


class TestListingIsNotOnePageOfEachBucket(McpTestCase):
    """Vikunja pages tasks inside a bucket and caps a page at 50.

    The board this was built against holds 170 tasks in one column and answers
    a request for 250 with 50, so "one call, whole board" is not a listing --
    it is the first page of each column, silently.
    """

    BACKLOG = 130
    layout = {
        "Backlog": [
            task(
                1000 + i,
                f"#{1000 + i} Backlog item {i}",
                "2026-07-26T05:00:00Z",
                index=999 + i,
            )
            for i in range(BACKLOG)
        ],
        "Ready": [
            task(9, "#33 Back up Vikunja database", "2026-07-26T05:05:50Z", index=8)
        ],
        "Done": [
            task(
                1,
                "#25 Wrap the reports step",
                "2026-07-26T04:59:00Z",
                done=True,
                index=2,
            )
        ],
    }

    def test_every_open_task_arrives_across_pages(self):
        listed = self.service.list_open_tasks()
        self.assertEqual(listed["count"], self.BACKLOG + 1)
        self.assertEqual(
            {t["vikunja_task_id"] for t in listed["tasks"]},
            {1000 + i for i in range(self.BACKLOG)} | {9},
        )

    def test_a_single_request_really_would_have_been_short(self):
        """Guards the test above: one page of the Backlog bucket stops at 50."""
        page = self.client._view_page(PROJECT_ID, VIEW_ID, 1, None)
        backlog = next(b for b in page if b["title"] == "Backlog")
        self.assertEqual(len(backlog["tasks"]), 50)
        self.assertLess(len(backlog["tasks"]), self.BACKLOG)
        self.assertEqual(backlog["count"], self.BACKLOG)

    def test_it_asked_for_more_than_one_page(self):
        self.service.list_open_tasks()
        pages = [path for _, path, _ in self.vikunja.calls if "page=" in path]
        self.assertGreater(len(pages), 1)


class TestAShortReadIsAnErrorNotAShorterBoard(McpTestCase):
    """The one failure a listing must never have is a quiet one."""

    class Truncating(FakeVikunja):
        """Serves one page and claims there were more. Nothing says which."""

        def _view_tasks(self, path: str, board: dict | None = None) -> list[dict]:
            served = super()._view_tasks(path, board)
            for bucket in served:
                if bucket["title"] == "Backlog":
                    bucket["count"] = bucket["count"] + 7
            return served

    def setUp(self) -> None:
        super().setUp()
        self.vikunja = self.Truncating()
        self.client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.service = McpService(self.config, self.client)

    def test_it_refuses_rather_than_returning_what_it_has(self):
        with self.assertRaises(ToolError) as caught:
            self.service.list_open_tasks()
        message = str(caught.exception)
        self.assertIn("silently omit", message)
        self.assertIn("Backlog", message)


class TestAVikunjaThatIgnoresTheFilter(McpTestCase):
    """`done = false` is sent to save a walk, never to decide the answer.

    A Vikunja that did not apply it would hand back the Done column too. The
    listing must still exclude those tasks -- and must not read its own second
    filtering as evidence that the server served short.
    """

    def setUp(self) -> None:
        super().setUp()
        self.vikunja = FilterIgnoringVikunja()
        self.client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.service = McpService(self.config, self.client)

    def test_the_done_task_is_still_excluded(self):
        listed = self.service.list_open_tasks()
        self.assertEqual({t["vikunja_task_id"] for t in listed["tasks"]}, {5, 9, 10, 11})

    def test_the_server_really_did_hand_over_the_done_task(self):
        """Guards the test above: without this the fake proves nothing."""
        served = self.client.list_all_tickets(PROJECT_ID, VIEW_ID)
        self.assertIn(1, {t.task_id for t in served})

    def test_filtering_client_side_is_not_mistaken_for_a_short_read(self):
        self.assertEqual(self.service.list_open_tasks()["count"], 4)


class TestListOpenTaskOrdering(McpTestCase):
    layout = {
        "Backlog": [
            task(30, "#30 Low", "2026-07-26T05:00:00Z", priority=1, index=29),
            task(20, "#20 Urgent, later id", "2026-07-26T05:00:00Z", priority=4, index=19),
            task(
                10, "#10 Urgent, earlier id", "2026-07-26T05:00:00Z",
                priority=4, index=9,
            ),
        ],
        "Ready": [
            task(40, "#40 Unset", "2026-07-26T05:00:00Z", index=39),
            task(5, "#5 Do now", "2026-07-26T05:00:00Z", priority=5, index=4),
        ],
        "Done": [
            task(1, "#1 Done", "2026-07-26T04:59:00Z", done=True, priority=5, index=2)
        ],
    }

    def order(self) -> list[int]:
        return [t["vikunja_task_id"] for t in self.service.list_open_tasks()["tasks"]]

    def test_most_urgent_first_then_by_task_id(self):
        self.assertEqual(self.order(), [5, 10, 20, 30, 40])

    def test_the_order_does_not_depend_on_which_bucket_a_task_sits_in(self):
        """5 and 40 share a column and land at opposite ends of the listing."""
        listed = self.order()
        self.assertEqual(listed[0], 5)
        self.assertEqual(listed[-1], 40)

    def test_repeating_the_call_gives_the_identical_order(self):
        self.assertEqual(self.order(), self.order())

    def test_priority_is_reported_as_the_number_and_as_a_name(self):
        first = self.service.list_open_tasks()["tasks"][0]
        self.assertEqual(first["priority"], 5)
        self.assertEqual(first["priority_label"], "do now")


class TestListOpenTaskFilters(McpTestCase):
    layout = {
        "Backlog": [
            task(5, "#29 Admin", "2026-07-26T05:01:00Z", labels=["Ops"], index=4),
            task(6, "#31 Unlabelled", "2026-07-26T05:02:00Z", index=5),
        ],
        "Ready": [
            task(9, "#33 Back up", "2026-07-26T05:05:50Z", labels=["Ops", "S7"], index=8),
            task(10, "#34 Version the skill", "2026-07-26T05:09:00Z", labels=["S7"], index=9),
        ],
        "In Progress": [],
        "Done": [
            task(1, "#25 Wrap", "2026-07-26T04:59:00Z", done=True, labels=["Ops"], index=2)
        ],
    }

    def ids(self, **filters) -> set[int]:
        return {t["vikunja_task_id"] for t in self.service.list_open_tasks(**filters)["tasks"]}

    def test_a_bucket_filter_narrows_to_that_column(self):
        self.assertEqual(self.ids(bucket="Ready"), {9, 10})

    def test_a_bucket_filter_is_matched_without_regard_to_case_or_padding(self):
        self.assertEqual(self.ids(bucket="  ready "), {9, 10})

    def test_the_filter_that_was_applied_is_reported_canonically(self):
        listed = self.service.list_open_tasks(bucket="ready")
        self.assertEqual(listed["filters"], {"bucket": "Ready", "label": None})

    def test_an_empty_column_is_an_empty_answer_not_a_refusal(self):
        listed = self.service.list_open_tasks(bucket="In Progress")
        self.assertEqual(listed["count"], 0)
        self.assertEqual(listed["tasks"], [])

    def test_an_unknown_bucket_is_refused_and_names_the_real_ones(self):
        """An empty list would read as 'nothing open there', which is a lie."""
        with self.assertRaises(ToolError) as caught:
            self.service.list_open_tasks(bucket="Redy")
        message = str(caught.exception)
        self.assertIn("Redy", message)
        self.assertIn("Ready", message)
        self.assertIn("Backlog", message)

    def test_a_label_filter_narrows_to_tasks_carrying_it(self):
        self.assertEqual(self.ids(label="S7"), {9, 10})
        self.assertEqual(self.ids(label="Ops"), {5, 9})

    def test_a_label_filter_is_matched_without_regard_to_case(self):
        self.assertEqual(self.ids(label="s7"), {9, 10})

    def test_a_label_filter_never_reaches_a_done_task(self):
        self.assertNotIn(1, self.ids(label="Ops"))

    def test_an_unused_label_narrows_to_nothing_and_says_what_exists(self):
        listed = self.service.list_open_tasks(label="Operatoins")
        self.assertEqual(listed["tasks"], [])
        self.assertEqual(listed["labels_in_use"], ["Ops", "S7"])

    def test_the_two_filters_compose(self):
        self.assertEqual(self.ids(bucket="Ready", label="Ops"), {9})

    def test_omitting_both_filters_returns_the_whole_open_board(self):
        self.assertEqual(self.ids(), {5, 6, 9, 10})

    def test_filtering_through_the_protocol_works_the_same(self):
        listed = self.call_tool("list_open_tasks", bucket="Ready", label="S7")
        found = listed["result"]["structuredContent"]
        self.assertEqual({t["vikunja_task_id"] for t in found["tasks"]}, {9, 10})


class TestTheListingCannotWrite(McpTestCase):
    """Stronger than "it issued no known mutation": it issued nothing but reads."""

    def test_every_call_the_listing_makes_is_a_read(self):
        self.service.list_open_tasks(bucket="Ready", label="Operations")
        methods = {method for method, _, _ in self.vikunja.calls}
        self.assertEqual(methods, {"GET"})

    def test_it_sends_no_request_body_at_all(self):
        self.service.list_open_tasks()
        self.assertEqual([body for _, _, body in self.vikunja.calls], [
            None for _ in self.vikunja.calls
        ])

    def test_the_board_is_identical_afterwards(self):
        before = deepcopy(self.vikunja.layout)
        self.service.list_open_tasks()
        self.service.list_open_tasks(bucket="Backlog")
        self.assertEqual(self.vikunja.layout, before)

    def test_the_tool_declares_itself_read_only(self):
        listing = {t.name: t for t in self.service.tools()}["list_open_tasks"]
        self.assertTrue(listing.annotations["readOnlyHint"])
        self.assertEqual(listing.required_arguments(), [])

    def test_no_argument_can_point_it_at_an_unapproved_project(self):
        """It names a board out of the approved set, and refuses any other.

        The refusal is what matters, not the absence of the argument: an
        unapproved id must not be quietly answered from the default board,
        because a caller that asked about project 1 would then be handed
        project 2's queue and told it was project 1's.
        """
        schema = {t.name: t for t in self.service.tools()}["list_open_tasks"].input_schema
        self.assertIn("project_id", schema["properties"])
        self.assertNotIn("project", schema["properties"])
        self.assertFalse(schema["additionalProperties"])

        with self.assertRaises(ToolError) as caught:
            self.service.list_open_tasks(project_id=OTHER_PROJECT_ID)
        self.assertIn(str(OTHER_PROJECT_ID), str(caught.exception))

    def test_every_path_it_reads_belongs_to_the_configured_project(self):
        self.service.list_open_tasks(bucket="Ready")
        read = [path for _, path, _ in self.vikunja.calls if path.startswith("/projects/")]
        self.assertTrue(read)
        for path in read:
            self.assertTrue(
                path.startswith(f"/projects/{PROJECT_ID}"),
                f"the listing read {path!r}, which is outside the allowed project",
            )


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
        self.assertEqual(result["url"], f"http://127.0.0.1:3456/tasks/{result['vikunja_task_id']}")
        self.assertEqual(self.stored(result["vikunja_task_id"])["title"], self.TITLE)

    def test_the_stored_description_is_the_one_that_was_asked_for(self):
        result = self.create()
        stored = self.stored(result["vikunja_task_id"])["description"]
        self.assertEqual(html_to_text(stored), self.BODY)

    def test_the_description_is_escaped_never_interpreted(self):
        result = self.create(description="Watch out for <script>alert(1)</script>")
        stored = self.stored(result["vikunja_task_id"])["description"]
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
        self.assertEqual(record["task_id"], result["vikunja_task_id"])
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
            self.service.get_task(8)
        self.assertIn("AI Alpha Engine", str(caught.exception))

    def test_the_create_fails_explicitly_and_creates_nothing(self):
        with self.assertRaises(ToolError) as caught:
            self.service.create_task(PROJECT_ID, "A ticket", "A body.")
        self.assertIn("Cannot resolve project", str(caught.exception))
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
        self.assertEqual(second["vikunja_task_id"], first["vikunja_task_id"])
        self.assertEqual(self.board_size(), before)

    def test_a_retry_after_a_restart_still_does_not_duplicate(self):
        """The ledger is on disk, so restarting does not reopen the door."""
        first = self.create()

        restarted = McpService(self.config, self.client)
        second = self.create(service=restarted)

        self.assertFalse(second["created"])
        self.assertEqual(second["vikunja_task_id"], first["vikunja_task_id"])

    def test_a_genuinely_different_ticket_is_created(self):
        first = self.create()
        second = self.service.create_task(PROJECT_ID, self.TITLE, "A different body.")
        self.assertTrue(second["created"])
        self.assertNotEqual(second["vikunja_task_id"], first["vikunja_task_id"])

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
        response = self.call_tool("get_task", task_number=8)
        self.assertEqual(response["result"]["structuredContent"]["vikunja_task_id"], 9)

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
        self.assertEqual(created["url"], f"http://127.0.0.1:3456/tasks/{created['vikunja_task_id']}")


class TestTheConfiguredIdsAreAuthoritative(unittest.TestCase):
    """Approved ids come from configuration and are never looked up by title."""

    def test_a_board_resolves_without_listing_every_project(self):
        """`GET /projects` is the listing that would show every board there is.

        Never asked: the approved ids are configuration, so resolution only
        ever fetches a board this connection was already allowed to see.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config = make_mcp_config(Path(tmp))
            vikunja = FakeVikunja()
            client = VikunjaClient(config.api_url, config.token, transport=vikunja)
            service = McpService(config, client)

            self.assertEqual(service._board().project_id, PROJECT_ID)
            self.assertEqual(
                service._board(TRADER_PROJECT_ID).project_id, TRADER_PROJECT_ID
            )
            self.assertNotIn("GET /projects", signatures(vikunja))


class TestTheWholeSurface(McpTestCase):
    def test_the_service_offers_exactly_the_advertised_operations(self):
        self.assertEqual(
            {tool.name for tool in self.service.tools()},
            VIKUNJA_TOOLS,
        )

    def test_driving_every_read_and_the_create_never_touches_an_existing_task(self):
        """Exercise everything but the two edits, then check nothing moved.

        Narrowed by task 196, which added `update_task` and `add_task_comment`
        — driving those *is* touching an existing task, which is the point of
        them. Everything else on the surface is still held to this, and the two
        that are not are held to `TestTheEditsAreTheOnlyThingThatWrites` below,
        which is stricter about them than a blanket sweep could be.
        """
        protocol = McpProtocol(self.service.tools())
        self.call_tool("get_task", task_number=8)
        self.call_tool("list_open_tasks")
        self.call_tool("list_open_tasks", bucket="Ready", label="Operations")
        self.call_tool("search_tasks", text="backup", status="any")
        self.call_tool(
            "create_task", project_id=PROJECT_ID, title="A ticket", description="A body."
        )

        self.assertEqual(
            protocol.tool_names, VIKUNJA_TOOLS
        )
        for signature in signatures(self.vikunja):
            for pattern, what in MUTATIONS:
                self.assertIsNone(
                    pattern.match(signature),
                    f"the boundary issued {signature!r}, which would {what}",
                )


class TestTheEditsAreTheOnlyThingThatWrites(McpTestCase):
    """Which tools may change an existing task, asserted as an exact set.

    A subset check would still pass the day `close_task` appears. This is the
    assertion task 196 has to leave behind: the write boundary moved, so it is
    pinned at where it moved to rather than deleted.
    """

    def annotations(self) -> dict[str, dict]:
        return {tool.name: tool.annotations for tool in self.service.tools()}

    def test_exactly_two_tools_can_change_an_existing_task(self):
        self.assertEqual(WRITE_TOOLS, {"update_task", "add_task_comment"})
        self.assertEqual(VIKUNJA_TOOLS - WRITE_TOOLS - {"create_task"}, READ_TOOLS)

    def test_every_other_tool_declares_itself_read_only(self):
        for name, annotations in self.annotations().items():
            with self.subTest(tool=name):
                self.assertEqual(
                    annotations["readOnlyHint"],
                    name in READ_TOOLS,
                    f"{name} declares readOnlyHint={annotations['readOnlyHint']}",
                )

    def test_only_the_edit_declares_itself_destructive(self):
        """A comment adds; an edit replaces text that was there."""
        destructive = {
            name
            for name, annotations in self.annotations().items()
            if annotations.get("destructiveHint")
        }
        self.assertEqual(destructive, {"update_task"})

    def test_no_write_tool_takes_an_argument_that_closes_moves_or_labels(self):
        """The writes name the two fields they may change and nothing else.

        `list_open_tasks` takes `bucket` and `label` as *filters*, which is why
        this is asked of the writes rather than of the whole surface — and why
        `additionalProperties: false` matters here: an unlisted field is what
        would let one of these carry `done` through to a whole-task replace.
        """
        forbidden = {"done", "status", "bucket", "bucket_id", "labels", "label_ids",
                     "assignees", "priority", "due_date", "position"}
        writes = [t for t in self.service.tools() if t.name in WRITE_TOOLS]
        self.assertEqual({t.name for t in writes}, WRITE_TOOLS)
        for tool in writes:
            with self.subTest(tool=tool.name):
                named = set(tool.input_schema.get("properties") or {})
                self.assertEqual(named & forbidden, set())
                self.assertFalse(tool.input_schema["additionalProperties"])

    def test_the_board_selector_selects_and_cannot_reproject_a_task(self):
        """`project_id` left the forbidden list when the writes gained it.

        It is there for a different reason from the fields above: those
        would change something about the task, and this one only says which
        approved board the task must already be on. The distinction is only
        worth anything if the write itself never carries it, so that is what
        is asserted — the task the edit sends back holds no project field,
        and nothing is POSTed to a project route.
        """
        for tool in [t for t in self.service.tools() if t.name in WRITE_TOOLS]:
            with self.subTest(tool=tool.name):
                self.assertIn("project_id", tool.input_schema["properties"])
                self.assertNotIn("project_id", tool.required_arguments())

        preview = self.service.update_task(8, title="A retitled ticket")
        self.service.update_task(
            8,
            title="A retitled ticket",
            approval_token=preview["approval_token"],
        )
        written = [
            (method, path, body)
            for method, path, body in self.vikunja.calls
            if method in ("POST", "PUT")
        ]
        self.assertTrue(written)
        for method, path, body in written:
            self.assertNotIn("/projects/", path)
            self.assertEqual(
                {k for k in (body or {}) if "project" in k}, set()
            )


if __name__ == "__main__":
    unittest.main()
