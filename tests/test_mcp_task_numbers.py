"""What a task tool means by a number, and what it refuses to mean.

Task 649. The connector used to address a task by Vikunja's immutable
``/tasks/<id>`` number while the board, and everyone reading it, uses the
project-local ``#N``. Those are different numbers for the same task — on the
live AI Alpha Engine board ``#647`` is task id 648 — so the two schemes overlap
almost completely and a confusion between them does not error. It returns a
real task, on the right board, with a plausible title, and reports success.

That is the failure every test here is aimed at, which is why the fixture has
**no task whose number equals its own id** and two numbers that are some other
task's id (see ``fakes.DEFAULT_LAYOUT``). An assertion that passed under either
reading would be worth nothing.
"""

from __future__ import annotations

import unittest

from vikunja_claude.mcp import ToolError
from vikunja_claude.mcp_service import McpService
from vikunja_claude.vikunja import AmbiguousTicket, TicketNotFound, VikunjaClient

from .fakes import (
    PROJECT_ID,
    VIEW_ID,
    FakeVikunja,
    FilterIgnoringVikunja,
    FilterMatchingNothingVikunja,
    task,
)
from .support import McpTestCase, make_mcp_config

#: The trap the whole change exists to remove: the board's #9 is task id 10,
#: and task id 9 is a *different* ticket, sitting at #8.
TRAP_NUMBER = 9
TRAP_TASK_ID = 10
DECOY_TASK_ID = 9

#: Every task-specific tool, and how to call it with a number.
TASK_TOOLS = {
    "get_task": {},
    "update_task": {"title": "Renamed"},
    "add_task_comment": {"comment": "A note."},
}


class TestANumberResolvesToTheTaskTheBoardShows(McpTestCase):
    def test_the_number_wins_over_the_identical_task_id(self):
        """#9 is task 10. Task 9 exists, is a different ticket, and is not it."""
        read = self.service.get_task(TRAP_NUMBER)

        self.assertEqual(read["task_number"], TRAP_NUMBER)
        self.assertEqual(read["vikunja_task_id"], TRAP_TASK_ID)
        self.assertNotEqual(read["vikunja_task_id"], DECOY_TASK_ID)
        self.assertIn("OpenClaw", read["title"])

    def test_the_decoy_really_is_a_different_task(self):
        """Guards the test above: without this it could be one task twice."""
        decoy = self.service.get_task(8)
        self.assertEqual(decoy["vikunja_task_id"], DECOY_TASK_ID)
        self.assertNotIn("OpenClaw", decoy["title"])

    def test_a_write_lands_on_the_task_the_number_names(self):
        preview = self.service.update_task(TRAP_NUMBER, title="Renamed by number")
        self.service.update_task(
            TRAP_NUMBER,
            title="Renamed by number",
            approval_token=preview["approval_token"],
        )

        self.assertEqual(
            self.vikunja._find(TRAP_TASK_ID)["title"], "Renamed by number"
        )
        self.assertNotEqual(
            self.vikunja._find(DECOY_TASK_ID)["title"], "Renamed by number"
        )

    def test_a_comment_lands_on_the_task_the_number_names(self):
        preview = self.service.add_task_comment(TRAP_NUMBER, comment="A note.")
        self.service.add_task_comment(
            TRAP_NUMBER,
            comment="A note.",
            approval_token=preview["approval_token"],
        )

        self.assertEqual(len(self.vikunja.comments.get(TRAP_TASK_ID, [])), 1)
        self.assertEqual(self.vikunja.comments.get(DECOY_TASK_ID, []), [])


class TestAnUnresolvedNumberIsRefusedNotReinterpreted(McpTestCase):
    """The refusal is the feature. Falling back would succeed, wrongly.

    #11 is on no task. Task id 11 *is* on the board — it is #10 — so a lookup
    that retried the number as an id would find something and answer with it.
    """

    ABSENT_NUMBER = 11
    EXISTING_TASK_ID = 11

    def test_the_id_it_would_have_fallen_back_to_is_really_there(self):
        """Guards every test in this class."""
        self.assertEqual(self.service.get_task(10)["vikunja_task_id"], 11)

    def test_every_task_tool_refuses_it(self):
        for name, extra in TASK_TOOLS.items():
            with self.subTest(tool=name):
                with self.assertRaises(ToolError) as caught:
                    getattr(self.service, name)(self.ABSENT_NUMBER, **extra)
                message = str(caught.exception)
                self.assertIn(f"#{self.ABSENT_NUMBER}", message)
                self.assertIn("AI Alpha Engine", message)

    def test_the_refusal_says_nothing_happened(self):
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(self.ABSENT_NUMBER, title="Renamed")
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(
            [c for c in self.vikunja.calls if c[0] in ("POST", "PUT")], []
        )

    def test_a_read_says_nothing_was_read_rather_than_nothing_was_changed(self):
        """A read that refuses did not half-succeed either, and says so in its
        own terms — "nothing was changed" would be true and beside the point."""
        with self.assertRaises(ToolError) as caught:
            self.service.get_task(self.ABSENT_NUMBER)
        self.assertIn("Nothing was read", str(caught.exception))


class TestTheImmutableIdIsNotAnArgument(McpTestCase):
    """A caller holding a /tasks/<id> number is told so, not quietly served."""

    def test_no_task_tool_advertises_task_id(self):
        by_name = {tool.name: tool for tool in self.service.tools()}
        for name in TASK_TOOLS:
            with self.subTest(tool=name):
                schema = by_name[name].input_schema
                self.assertIn("task_number", schema["properties"])
                self.assertNotIn("task_id", schema["properties"])
                self.assertEqual(
                    [a for a in by_name[name].required_arguments()
                     if a.endswith("task_number") or a == "task_id"],
                    ["task_number"],
                )

    def test_sending_only_task_id_is_a_bad_request_naming_the_field(self):
        """A client on the old contract is told which field to send.

        This is the protocol's own required-argument check, which is why the
        answer is a JSON-RPC error rather than a tool result — and why it is
        worth asserting: an argument the schema does not declare is otherwise
        simply ignored, and the call would fail somewhere less legible.
        """
        for name, extra in TASK_TOOLS.items():
            with self.subTest(tool=name):
                response = self.call_tool(name, task_id=9, **extra)
                self.assertIn("task_number", response["error"]["message"])

    def test_sending_only_task_id_never_reaches_vikunja(self):
        """Refused before the lookup, not after: no request goes out."""
        self.call_tool("update_task", task_id=9, title="Renamed")
        self.assertEqual(self.vikunja.calls, [])

    def test_sending_both_is_refused_rather_than_resolved(self):
        """The dangerous shape, and the one the required check cannot catch:
        a client that adds task_number and keeps task_id satisfies the schema.
        Ignoring the extra field would be right about half the time and
        silently wrong the rest, so the pair is refused outright."""
        for name, extra in TASK_TOOLS.items():
            with self.subTest(tool=name):
                result = self.call_tool(
                    name, task_number=8, task_id=9, **extra
                )["result"]
                self.assertTrue(result["isError"])
                self.assertIn("task_number", result["content"][0]["text"])

    def test_sending_both_writes_nothing(self):
        self.call_tool("update_task", task_number=8, task_id=9, title="Renamed")
        self.assertEqual(
            [c for c in self.vikunja.calls if c[0] in ("POST", "PUT")], []
        )

    def test_omitting_the_number_entirely_is_a_bad_request(self):
        response = self.call_tool("get_task")
        self.assertIn("task_number", response["error"]["message"])


class TestEveryAnswerNamesTheNumber(McpTestCase):
    """A caller must be able to call again with what it was handed back."""

    def test_a_read_carries_the_number_and_the_id_under_its_own_name(self):
        read = self.service.get_task(8)
        self.assertEqual(read["task_number"], 8)
        self.assertEqual(read["reference"], "#8")
        self.assertEqual(read["vikunja_task_id"], 9)
        self.assertNotIn("task_id", read)

    def test_a_listing_carries_the_number_for_every_task(self):
        listed = self.service.list_open_tasks()
        self.assertEqual(
            {t["task_number"] for t in listed["tasks"]}, {4, 8, 9, 10}
        )
        for entry in listed["tasks"]:
            self.assertNotIn("task_id", entry)
            self.assertEqual(entry["reference"], f"#{entry['task_number']}")

    def test_a_search_carries_the_number(self):
        found = self.service.search_tasks("Vikunja", status="any")
        self.assertTrue(found["tasks"])
        for entry in found["tasks"]:
            self.assertIn("task_number", entry)
            self.assertNotIn("task_id", entry)

    def test_a_listing_is_ordered_by_something_it_published(self):
        """Ordered on the number, which the caller can see — an order keyed on
        a field that is not in the answer is not an order the reader can read."""
        numbers = [t["task_number"] for t in self.service.list_open_tasks()["tasks"]]
        self.assertEqual(numbers, sorted(numbers))

    def test_a_create_reports_the_number_the_board_will_show(self):
        created = self.service.create_task(PROJECT_ID, "A new ticket", "A body.")
        self.assertTrue(created["created"])
        # Next on this board, not next id: ids run across every project.
        self.assertEqual(created["task_number"], 11)
        self.assertNotEqual(created["task_number"], created["vikunja_task_id"])
        self.assertEqual(
            self.service.get_task(created["task_number"])["vikunja_task_id"],
            created["vikunja_task_id"],
        )

    def test_a_repeated_create_reports_the_same_number(self):
        first = self.service.create_task(PROJECT_ID, "A new ticket", "A body.")
        second = self.service.create_task(PROJECT_ID, "A new ticket", "A body.")
        self.assertFalse(second["created"])
        self.assertEqual(second["task_number"], first["task_number"])

    def test_a_ledger_entry_written_before_this_change_still_reports_one(self):
        """The dedup ledger is on disk and outlives the contract that wrote it.

        Entries from before task 649 carry only the immutable id, so the number
        is looked up from the board for those rather than reported as null —
        which would make an idempotent replay look like a ticket with no number
        and invite the caller to file the duplicate the ledger exists to stop.
        """
        import json

        created = self.service.create_task(PROJECT_ID, "A new ticket", "A body.")
        path = self.config.ledger_path
        legacy = [
            {k: v for k, v in json.loads(line).items() if k != "task_number"}
            for line in path.read_text().splitlines()
            if line.strip()
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in legacy))
        self.assertNotIn("task_number", legacy[0])

        replayed = McpService(self.config, self.client).create_task(
            PROJECT_ID, "A new ticket", "A body."
        )
        self.assertFalse(replayed["created"])
        self.assertIsNotNone(replayed["task_number"])
        self.assertEqual(replayed["task_number"], created["task_number"])


class TestAResolutionThatCannotBeTrustedIsRefused(McpTestCase):
    """Two tasks answering to one number: say which, do not pick one."""

    layout = {
        "Backlog": [
            task(60, "First claimant", "2026-07-26T05:00:00Z", index=7),
            task(61, "Second claimant", "2026-07-26T05:01:00Z", index=7),
        ],
        "Ready": [],
        "In Progress": [],
        "Waiting": [],
        "Done": [],
    }

    def test_the_client_refuses_rather_than_taking_the_first(self):
        with self.assertRaises(AmbiguousTicket) as caught:
            self.client.find_by_task_number(7, PROJECT_ID, VIEW_ID)
        message = str(caught.exception)
        self.assertIn("60", message)
        self.assertIn("61", message)

    def test_a_write_against_it_changes_nothing(self):
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(7, title="Renamed")
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(
            [c for c in self.vikunja.calls if c[0] in ("POST", "PUT")], []
        )


class TestTheServerFilterIsAnOptimisationNotTheAnswer(unittest.TestCase):
    """``index = N`` saves a walk. It must never be what makes a lookup right.

    Same reasoning as the lookup by id: a Vikunja that ignores the filter is
    harmless, because the walk covers the board anyway. The one that matters is
    a filter that is applied and matches nothing — the reply is well-formed and
    internally consistent, and reading "no such task" out of it is the bug.
    """

    def service_over(self, transport) -> McpService:
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name))
        client = VikunjaClient(config.api_url, config.token, transport=transport)
        return McpService(config, client)

    def test_a_vikunja_that_ignores_the_filter_still_resolves(self):
        service = self.service_over(FilterIgnoringVikunja())
        self.assertEqual(service.get_task(TRAP_NUMBER)["vikunja_task_id"], TRAP_TASK_ID)

    def test_a_filter_matching_nothing_falls_back_to_the_whole_board(self):
        service = self.service_over(FilterMatchingNothingVikunja())
        self.assertEqual(service.get_task(TRAP_NUMBER)["vikunja_task_id"], TRAP_TASK_ID)

    def test_a_task_that_is_genuinely_absent_is_still_absent(self):
        """Guards the two above: the fallback must not answer everything."""
        service = self.service_over(FakeVikunja())
        with self.assertRaises(ToolError):
            service.get_task(4242)


class TestANumberVikunjaDidNotGiveIsNotInvented(unittest.TestCase):
    """A task with no usable ``index`` is unaddressable, and says so."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = make_mcp_config(Path(tmp.name))
        # `index: 0` is Vikunja's zero value for a field the reply left out.
        # Reporting it as #0 would name a task that cannot be looked up.
        unnumbered = task(70, "No number at all", "2026-07-26T05:00:00Z", index=8)
        unnumbered["index"] = 0
        self.vikunja = FakeVikunja(
            layout={"Backlog": [unnumbered], "Ready": [], "In Progress": [],
                    "Waiting": [], "Done": []}
        )
        client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.service = McpService(self.config, client)

    def test_it_is_listed_with_a_null_number_rather_than_a_wrong_one(self):
        listed = self.service.list_open_tasks()["tasks"]
        self.assertEqual([t["task_number"] for t in listed], [None])
        self.assertEqual(listed[0]["vikunja_task_id"], 70)

    def test_its_reference_says_which_number_it_is_quoting(self):
        listed = self.service.list_open_tasks()["tasks"]
        self.assertIn("task 70", listed[0]["reference"])
        self.assertNotEqual(listed[0]["reference"], "#0")

    def test_zero_is_refused_as_an_argument(self):
        with self.assertRaises(ToolError):
            self.service.get_task(0)

    def test_it_cannot_be_reached_by_its_id_either(self):
        with self.assertRaises(ToolError):
            self.service.get_task(70)


class TestTheLookupsAreNotEachOther(unittest.TestCase):
    """Both resolvers survive, because both questions are still asked.

    ``find_by_task_id`` is what the launcher page and ``vkctl`` use — they
    address a task by the URL a human pasted. ``find_by_task_number`` is what
    the MCP uses. Collapsing them would make one of the two surfaces wrong.
    """

    def setUp(self) -> None:
        from pathlib import Path
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name))
        self.client = VikunjaClient(
            config.api_url, config.token, transport=FakeVikunja()
        )

    def test_the_same_integer_answers_two_different_tasks(self):
        by_id = self.client.find_by_task_id(TRAP_NUMBER, PROJECT_ID, VIEW_ID)
        by_number = self.client.find_by_task_number(TRAP_NUMBER, PROJECT_ID, VIEW_ID)

        self.assertEqual(by_id.task_id, DECOY_TASK_ID)
        self.assertEqual(by_number.task_id, TRAP_TASK_ID)
        self.assertNotEqual(by_id.task_id, by_number.task_id)

    def test_each_ticket_carries_both_of_its_numbers(self):
        found = self.client.find_by_task_number(TRAP_NUMBER, PROJECT_ID, VIEW_ID)
        self.assertEqual(found.task_number, TRAP_NUMBER)
        self.assertEqual(found.task_id, TRAP_TASK_ID)
        self.assertEqual(found.board_reference, f"#{TRAP_NUMBER}")

    def test_a_number_below_one_is_refused_without_a_request(self):
        transport = FakeVikunja()
        client = VikunjaClient("http://x", "t", transport=transport)
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(TicketNotFound):
                    client.find_by_task_number(value, PROJECT_ID, VIEW_ID)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
