"""The boundary serves two approved boards, and nothing else.

The property under test is not "project 3 works". It is that the set of boards
this connection can reach is *closed and configured*, that a caller says which
one it means rather than having it guessed from a task id, and that everything
the single-board surface guaranteed still holds on each board separately.

Four ways that could go wrong, and one class here for each:

* a board outside the configured set becomes reachable;
* a call that names one board is answered from another — the failure a silent
  fallback to the default would produce, which reads as a correct answer;
* an id in the configured set stops naming the board it was approved for;
* the two-step approval on the writes loosens now that a write also names a
  board.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vikunja_claude.config import ConfigError, McpConfig, ProjectRef
from vikunja_claude.mcp import ToolError
from vikunja_claude.mcp_service import McpService
from vikunja_claude.vikunja import VikunjaClient, VikunjaError

from .fakes import (
    PROJECT_ID,
    PROJECT_TITLE,
    TRADER_PROJECT_ID,
    TRADER_TITLE,
    TRADER_VIEW_ID,
    VIEW_ID,
    FakeVikunja,
)
from .support import ISSUER, PASSPHRASE, REDIRECT_URI, TOKEN, McpTestCase, make_mcp_config

#: A task on each board, by both of its numbers. The **ids** are distinct, as
#: Vikunja's are, so a board asked for another board's id misses rather than
#: colliding with something real.
#:
#: The **numbers** are not distinct, and that is the point: they are handed out
#: per project, so #2 is a real task on each board and a different one on each.
#: A boundary that resolved a number without its board would not error here —
#: it would return the other board's ticket and look right.
ENGINE_TASK = 9
ENGINE_NUMBER = 8
TRADER_TASK = 40
TRADER_NUMBER = 2
TRADER_DONE_TASK = 39
TRADER_DONE_NUMBER = 1

#: #2 exists on both boards and means a different task on each.
SHARED_NUMBER = 2
ENGINE_SHARED_TASK = 1
TRADER_SHARED_TASK = TRADER_TASK

#: A number only one of the boards has, which is what makes a refusal a
#: refusal rather than an absence: #10 is the Engine board's task 11, and #3
#: is the Trader board's task 41.
ENGINE_ONLY_NUMBER = 10
TRADER_ONLY_NUMBER = 3
UNAPPROVED_PROJECT = 1


def writes(vikunja: FakeVikunja) -> list[tuple]:
    """Every call that could have changed something."""
    return [call for call in vikunja.calls if call[0] in ("POST", "PUT")]


class TestTheDefaultBoardIsUnchanged(McpTestCase):
    """Project 2 answers exactly as it did before there was a project 3.

    Both forms are checked — the omitted argument and the explicit `2` — because
    the compatibility claim is that they are the same call, not merely that each
    one works.
    """

    def test_a_read_that_names_no_board_is_answered_by_the_default(self):
        task = self.service.get_task(ENGINE_NUMBER)
        self.assertEqual(task["project_id"], PROJECT_ID)
        self.assertEqual(task["project"], PROJECT_TITLE)

    def test_naming_the_default_explicitly_is_the_same_call(self):
        self.assertEqual(
            self.service.get_task(ENGINE_NUMBER),
            self.service.get_task(ENGINE_NUMBER, project_id=PROJECT_ID),
        )
        self.assertEqual(
            self.service.list_open_tasks(),
            self.service.list_open_tasks(project_id=PROJECT_ID),
        )
        self.assertEqual(
            self.service.search_tasks("Vikunja", status="any"),
            self.service.search_tasks(
                "Vikunja", status="any", project_id=PROJECT_ID
            ),
        )

    def test_the_tool_names_did_not_change(self):
        """Existing callers address these by name; renaming one would break them."""
        self.assertLessEqual(
            {"get_task", "list_open_tasks", "search_tasks", "create_task",
             "update_task", "add_task_comment"},
            {tool.name for tool in self.service.tools()},
        )

    def test_the_board_selector_is_optional_on_every_tool_but_the_create(self):
        required = {
            tool.name: tool.required_arguments() for tool in self.service.tools()
        }
        for name in ("get_task", "list_open_tasks", "search_tasks",
                     "update_task", "add_task_comment"):
            with self.subTest(tool=name):
                self.assertNotIn("project_id", required[name])
        # The create is the exception, and deliberately: a ticket filed on the
        # wrong board is in front of real people and cannot be taken back.
        self.assertIn("project_id", required["create_task"])


class TestTheSecondBoardIsReadable(McpTestCase):
    """AI Alpha Trader, through the same three reads, named explicitly."""

    def test_one_task(self):
        task = self.service.get_task(TRADER_NUMBER, project_id=TRADER_PROJECT_ID)
        self.assertEqual(
            self.vikunja.id_of(task["task_number"], TRADER_PROJECT_ID), TRADER_TASK)
        self.assertEqual(task["project_id"], TRADER_PROJECT_ID)
        self.assertEqual(task["project"], TRADER_TITLE)
        self.assertIn("execution architecture", task["title"])
        self.assertIn("orders reach a broker", task["description"])

    def test_the_open_queue(self):
        listed = self.service.list_open_tasks(project_id=TRADER_PROJECT_ID)
        self.assertEqual(listed["project_id"], TRADER_PROJECT_ID)
        self.assertEqual(listed["project"], TRADER_TITLE)
        self.assertEqual(
            {self.vikunja.id_of(t["task_number"], TRADER_PROJECT_ID)
             for t in listed["tasks"]},
            {TRADER_TASK, 41},
        )
        # Its own count, not the Engine board's, and the done task is excluded.
        self.assertEqual(listed["count"], 2)

    def test_a_search_of_its_own_history(self):
        found = self.service.search_tasks(
            "venue", status="any", project_id=TRADER_PROJECT_ID
        )
        self.assertEqual(found["project_id"], TRADER_PROJECT_ID)
        self.assertEqual(
            [self.vikunja.id_of(t["task_number"], TRADER_PROJECT_ID)
             for t in found["tasks"]],
            [TRADER_DONE_TASK],
        )

    def test_its_buckets_are_its_own(self):
        """A column the Engine board has and this one does not is refused."""
        with self.assertRaises(ToolError) as caught:
            self.service.list_open_tasks(
                bucket="Waiting", project_id=TRADER_PROJECT_ID
            )
        self.assertIn(TRADER_TITLE, str(caught.exception))

    def test_the_reads_go_to_its_own_view(self):
        self.service.list_open_tasks(project_id=TRADER_PROJECT_ID)
        paths = [path.split("?")[0] for _, path, _ in self.vikunja.calls]
        self.assertIn(
            f"/projects/{TRADER_PROJECT_ID}/views/{TRADER_VIEW_ID}/tasks", paths
        )
        self.assertNotIn(f"/projects/{PROJECT_ID}/views/{VIEW_ID}/tasks", paths)


class TestOneBoardIsNeverAnsweredFromAnother(McpTestCase):
    """The view ids differ, and each board must be read through its own.

    This is the failure a single cached view id produced: the second board's
    request built from the first board's view, answering with the first board's
    tasks under the second board's name. Nothing downstream could tell that from
    a right answer, so it is asserted rather than assumed.
    """

    def test_resolving_one_board_does_not_pin_the_other(self):
        first = self.service._board(PROJECT_ID)
        second = self.service._board(TRADER_PROJECT_ID)
        self.assertEqual(first.view_id, VIEW_ID)
        self.assertEqual(second.view_id, TRADER_VIEW_ID)
        self.assertNotEqual(first.view_id, second.view_id)

    def test_reading_one_board_then_the_other_returns_different_tasks(self):
        engine = self.service.list_open_tasks()
        trader = self.service.list_open_tasks(project_id=TRADER_PROJECT_ID)
        self.assertEqual(
            {self.vikunja.id_of(t["task_number"]) for t in engine["tasks"]}
            & {self.vikunja.id_of(t["task_number"], TRADER_PROJECT_ID)
               for t in trader["tasks"]},
            set(),
        )

    def test_the_order_of_resolution_does_not_change_the_answer(self):
        trader_first = self.service.list_open_tasks(project_id=TRADER_PROJECT_ID)
        fresh = McpService(
            self.config,
            VikunjaClient(
                self.config.api_url, self.config.token, transport=FakeVikunja()
            ),
        )
        fresh.list_open_tasks()
        self.assertEqual(
            trader_first["tasks"],
            fresh.list_open_tasks(project_id=TRADER_PROJECT_ID)["tasks"],
        )


class TestATaskMustBeOnTheBoardThatWasNamed(McpTestCase):
    """Ownership is never inferred from a task id.

    Vikunja task ids are unique across projects, so "this id exists" would be
    enough to serve any task on any board. That is exactly the inference this
    boundary must not make: the lookup goes through the named board's view, and
    a task that is not in it is a refusal, in both directions.
    """

    def test_the_default_board_does_not_serve_the_other_boards_task(self):
        with self.assertRaises(ToolError) as caught:
            self.service.get_task(TRADER_ONLY_NUMBER)
        message = str(caught.exception)
        self.assertIn(str(TRADER_ONLY_NUMBER), message)
        self.assertIn(PROJECT_TITLE, message)
        # And it says where to look, rather than only that it will not.
        self.assertIn(TRADER_TITLE, message)

    def test_the_second_board_does_not_serve_the_default_boards_task(self):
        with self.assertRaises(ToolError) as caught:
            self.service.get_task(ENGINE_ONLY_NUMBER, project_id=TRADER_PROJECT_ID)
        self.assertIn(TRADER_TITLE, str(caught.exception))

    def test_one_number_on_two_boards_is_two_tasks(self):
        """The sharper form of the two refusals above.

        A missing number refuses loudly, which is the easy case. This is the
        quiet one: #2 exists on both boards, so a resolver that dropped the
        project would return *a* real task with a plausible title and no error
        at all. Nothing but the board decides which of the two answers.
        """
        engine = self.service.get_task(SHARED_NUMBER, project_id=PROJECT_ID)
        trader = self.service.get_task(SHARED_NUMBER, project_id=TRADER_PROJECT_ID)

        self.assertEqual(engine["task_number"], SHARED_NUMBER)
        self.assertEqual(trader["task_number"], SHARED_NUMBER)
        # One number, two boards, two rows — asked of each store, since the
        # answer names the board and the number and nothing else (task 660).
        self.assertEqual(
            self.vikunja.id_of(engine["task_number"]), ENGINE_SHARED_TASK)
        self.assertEqual(
            self.vikunja.id_of(trader["task_number"], TRADER_PROJECT_ID),
            TRADER_SHARED_TASK)
        self.assertNotEqual(engine["title"], trader["title"])

    def test_the_default_board_answers_the_shared_number_with_its_own(self):
        """Omitting project_id is the default board, not "whichever has one"."""
        self.assertEqual(
            self.service.get_task(SHARED_NUMBER),
            self.service.get_task(SHARED_NUMBER, project_id=PROJECT_ID),
        )

    def test_an_edit_naming_the_wrong_board_changes_nothing(self):
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(
                TRADER_ONLY_NUMBER, title="Renamed", project_id=PROJECT_ID
            )
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(writes(self.vikunja), [])

    def test_an_edit_on_a_shared_number_lands_on_the_board_that_was_named(self):
        """The dangerous case for a write: both boards have a #2."""
        preview = self.service.update_task(
            SHARED_NUMBER, title="Renamed on the trader board",
            project_id=TRADER_PROJECT_ID,
        )
        self.service.update_task(
            SHARED_NUMBER,
            title="Renamed on the trader board",
            approval_token=preview["approval_token"],
            project_id=TRADER_PROJECT_ID,
        )
        self.assertEqual(
            self.vikunja._find(TRADER_SHARED_TASK)["title"],
            "Renamed on the trader board",
        )
        self.assertNotEqual(
            self.vikunja._find(ENGINE_SHARED_TASK)["title"],
            "Renamed on the trader board",
        )

    def test_a_comment_naming_the_wrong_board_writes_nothing(self):
        with self.assertRaises(ToolError) as caught:
            self.service.add_task_comment(
                ENGINE_ONLY_NUMBER, "A note.", project_id=TRADER_PROJECT_ID
            )
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(writes(self.vikunja), [])

    def test_a_create_cannot_be_redirected_by_naming_the_wrong_board(self):
        """The create is not a lookup, so its guard is its own: an id it was
        given is either approved or refused, never substituted."""
        created = self.service.create_task(
            TRADER_PROJECT_ID, "A trader ticket", "A body."
        )
        self.assertEqual(created["project_id"], TRADER_PROJECT_ID)
        self.assertEqual(created["project"], TRADER_TITLE)
        self.assertEqual(
            self.vikunja.bucket_of(
                self.vikunja.id_of(created["task_number"], TRADER_PROJECT_ID),
                TRADER_PROJECT_ID),
            "Backlog",
        )
        self.assertNotIn("vikunja_task_id", created)


class TestAnUnapprovedProjectIsUnreachable(McpTestCase):
    """The set is closed. Not narrowed, not defaulted — refused."""

    def test_every_task_tool_refuses_a_project_outside_the_set(self):
        calls = {
            "get_task": lambda pid: self.service.get_task(
                ENGINE_NUMBER, project_id=pid
            ),
            "list_open_tasks": lambda pid: self.service.list_open_tasks(project_id=pid),
            "search_tasks": lambda pid: self.service.search_tasks("a", project_id=pid),
            "update_task": lambda pid: self.service.update_task(
                ENGINE_NUMBER, title="Renamed", project_id=pid
            ),
            "add_task_comment": lambda pid: self.service.add_task_comment(
                ENGINE_NUMBER, "A note.", project_id=pid
            ),
            "create_task": lambda pid: self.service.create_task(pid, "T", "B"),
        }
        for name, call in calls.items():
            for project_id in (UNAPPROVED_PROJECT, 99, -1):
                with self.subTest(tool=name, project_id=project_id):
                    with self.assertRaises(ToolError) as caught:
                        call(project_id)
                    message = str(caught.exception)
                    self.assertIn(str(project_id), message)
                    self.assertIn(PROJECT_TITLE, message)
                    self.assertIn(TRADER_TITLE, message)

    def test_a_refused_project_is_never_read_from(self):
        """Refused before any request, so the Inbox is not even fetched."""
        with self.assertRaises(ToolError):
            self.service.list_open_tasks(project_id=UNAPPROVED_PROJECT)
        self.assertEqual(
            [
                path
                for _, path, _ in self.vikunja.calls
                if path.startswith(f"/projects/{UNAPPROVED_PROJECT}")
            ],
            [],
        )

    def test_a_refused_create_creates_nothing(self):
        before = sum(len(tasks) for tasks in self.vikunja.layout.values())
        with self.assertRaises(ToolError) as caught:
            self.service.create_task(UNAPPROVED_PROJECT, "A ticket", "A body.")
        self.assertIn("Nothing was created", str(caught.exception))
        self.assertEqual(
            sum(len(tasks) for tasks in self.vikunja.layout.values()), before
        )
        self.assertFalse(self.config.ledger_path.exists())


class TestAnApprovedIdMustStillNameItsBoard(unittest.TestCase):
    """Configuration says id 3 is AI Alpha Trader. Vikunja is asked to agree.

    An id is only a promise about a board while the board on the other end is
    the one it was approved for. A project deleted and recreated, or renumbered,
    would leave the id pointing at something nobody approved — and every check
    downstream would still pass, because they all check the id.
    """

    def service(self, vikunja: FakeVikunja) -> McpService:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = make_mcp_config(Path(tmp.name))
        return McpService(
            config,
            VikunjaClient(config.api_url, config.token, transport=vikunja),
        )

    def test_a_renamed_board_is_refused_rather_than_read(self):
        vikunja = FakeVikunja(
            projects={
                TRADER_PROJECT_ID: {
                    "title": "Somebody else's board",
                    "view_id": TRADER_VIEW_ID,
                    "buckets": [{"id": 20, "title": "Backlog"}],
                    "layout": {"Backlog": []},
                }
            }
        )
        with self.assertRaises(ToolError) as caught:
            self.service(vikunja).list_open_tasks(project_id=TRADER_PROJECT_ID)
        message = str(caught.exception)
        self.assertIn("Somebody else's board", message)
        self.assertIn(TRADER_TITLE, message)

    def test_a_board_that_is_not_there_fails_explicitly(self):
        vikunja = FakeVikunja(
            projects={
                PROJECT_ID: {
                    "title": PROJECT_TITLE,
                    "view_id": VIEW_ID,
                    "buckets": [{"id": 10, "title": "Backlog"}],
                    "layout": {"Backlog": []},
                }
            }
        )
        with self.assertRaises(ToolError) as caught:
            self.service(vikunja).get_task(
                TRADER_TASK, project_id=TRADER_PROJECT_ID
            )
        self.assertIn(str(TRADER_PROJECT_ID), str(caught.exception))


class TestTheApprovalFlowIsUnchangedOnBothBoards(McpTestCase):
    """The two-step write is the same two steps, on whichever board.

    Nothing here is new behaviour — it is the task 196 flow asserted again with
    a board named, because the argument that selects the board is on the same
    call that carries the approval.
    """

    def test_a_preview_on_the_second_board_writes_nothing(self):
        preview = self.service.update_task(
            TRADER_NUMBER, title="A renamed trader ticket",
            project_id=TRADER_PROJECT_ID,
        )
        self.assertTrue(preview["approval_required"])
        self.assertFalse(preview["applied"])
        self.assertEqual(preview["project_id"], TRADER_PROJECT_ID)
        self.assertEqual(preview["current"]["title"], "Define the execution architecture")
        self.assertEqual(writes(self.vikunja), [])

    def test_an_approved_edit_on_the_second_board_applies(self):
        preview = self.service.update_task(
            TRADER_NUMBER, title="A renamed trader ticket",
            project_id=TRADER_PROJECT_ID,
        )
        applied = self.service.update_task(
            TRADER_NUMBER,
            title="A renamed trader ticket",
            approval_token=preview["approval_token"],
            project_id=TRADER_PROJECT_ID,
        )
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["project_id"], TRADER_PROJECT_ID)
        self.assertEqual(
            self.service.get_task(TRADER_NUMBER, project_id=TRADER_PROJECT_ID)["title"],
            "A renamed trader ticket",
        )

    def test_a_comment_on_the_second_board_still_needs_its_token(self):
        preview = self.service.add_task_comment(
            TRADER_NUMBER, "A trader note.", project_id=TRADER_PROJECT_ID
        )
        self.assertTrue(preview["approval_required"])
        self.assertEqual(writes(self.vikunja), [])

        added = self.service.add_task_comment(
            TRADER_NUMBER,
            "A trader note.",
            approval_token=preview["approval_token"],
            project_id=TRADER_PROJECT_ID,
        )
        self.assertTrue(added["added"])
        self.assertEqual(added["project_id"], TRADER_PROJECT_ID)

    def test_a_token_cannot_be_redeemed_against_the_other_board(self):
        """The board is re-resolved on the commit, so the task must still be on it.

        The refusal comes from ownership rather than from the token, which is
        the right order: the token describes a change to a task, and a task that
        is not on the board named is not this connection's to change at all.
        """
        preview = self.service.update_task(
            TRADER_NUMBER, title="A renamed trader ticket",
            project_id=TRADER_PROJECT_ID,
        )
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(
                TRADER_NUMBER,
                title="A renamed trader ticket",
                approval_token=preview["approval_token"],
                project_id=PROJECT_ID,
            )
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(writes(self.vikunja), [])
        self.assertEqual(
            self.service.get_task(TRADER_NUMBER, project_id=TRADER_PROJECT_ID)["title"],
            "Define the execution architecture",
        )

    def test_a_wrong_token_still_refuses_on_the_second_board(self):
        self.service.update_task(
            TRADER_NUMBER, title="A renamed trader ticket",
            project_id=TRADER_PROJECT_ID,
        )
        with self.assertRaises(ToolError):
            self.service.update_task(
                TRADER_NUMBER,
                title="A renamed trader ticket",
                approval_token="not-a-token",
                project_id=TRADER_PROJECT_ID,
            )
        self.assertEqual(writes(self.vikunja), [])

    def test_a_ticket_filed_twice_on_the_second_board_is_not_duplicated(self):
        first = self.service.create_task(TRADER_PROJECT_ID, "A ticket", "A body.")
        second = self.service.create_task(TRADER_PROJECT_ID, "A ticket", "A body.")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["task_number"], second["task_number"])

    def test_the_same_ticket_on_the_other_board_is_a_different_ticket(self):
        """The board is part of what a creation request *is*.

        Otherwise the second board's first ticket would be silently answered
        with the first board's, and nothing would be filed.
        """
        engine = self.service.create_task(PROJECT_ID, "A ticket", "A body.")
        trader = self.service.create_task(TRADER_PROJECT_ID, "A ticket", "A body.")
        self.assertTrue(engine["created"])
        self.assertTrue(trader["created"])
        # Same number is possible on two boards; the BOARD is what differs.
        self.assertNotEqual(
            (engine["project_id"], engine["task_number"]),
            (trader["project_id"], trader["task_number"]))


class TestTheApprovedSetIsConfiguration(unittest.TestCase):
    """Widening the boundary is an environment change, never an argument."""

    def from_env(self, **overrides) -> McpConfig:
        environment = {
            "VIKUNJA_API_TOKEN": TOKEN,
            "VIKUNJA_MCP_OAUTH_ISSUER": ISSUER,
            "VIKUNJA_MCP_OAUTH_PASSPHRASE": PASSPHRASE,
            "VIKUNJA_MCP_OAUTH_REDIRECT_URIS": REDIRECT_URI,
        }
        environment.update({k: v for k, v in overrides.items() if v is not None})
        with mock.patch.dict("os.environ", environment, clear=True):
            return McpConfig.from_env(env_file=None)

    def test_the_default_is_the_two_ai_alpha_boards(self):
        config = self.from_env()
        self.assertEqual(config.allowed_project_ids, (2, 3))
        self.assertEqual(config.default_project, ProjectRef(2, "AI Alpha Engine"))
        self.assertEqual(config.project_for(3).title, "AI Alpha Trader")
        self.assertIsNone(config.project_for(1))

    def test_the_set_can_be_named_in_the_environment(self):
        config = self.from_env(VIKUNJA_MCP_PROJECTS="5:Ops , 6:Research")
        self.assertEqual(config.allowed_project_ids, (5, 6))
        self.assertEqual(config.project_for(6).title, "Research")

    def test_an_entry_that_names_no_id_or_no_title_is_refused(self):
        for value in ("Ops", "5", "5:", ":Ops", "5:Ops,seven:Research"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as caught:
                    self.from_env(VIKUNJA_MCP_PROJECTS=value)
                self.assertIn("VIKUNJA_MCP_PROJECTS", str(caught.exception))

    def test_the_same_id_cannot_be_named_twice(self):
        with self.assertRaises(ConfigError):
            self.from_env(VIKUNJA_MCP_PROJECTS="5:Ops,5:Research")

    def test_a_set_that_names_nothing_is_refused_rather_than_emptied(self):
        with self.assertRaises(ConfigError):
            self.from_env(VIKUNJA_MCP_PROJECTS=" , ")

    def test_the_phrase_the_tools_publish_is_the_set_they_enforce(self):
        config = self.from_env()
        self.assertEqual(
            config.projects_phrase, "AI Alpha Engine (2) and AI Alpha Trader (3)"
        )
        for project in config.projects:
            self.assertIn(str(project.project_id), config.projects_phrase)
            self.assertIn(project.title, config.projects_phrase)


class TestTheClientAnswersEachBoardsQuestion(unittest.TestCase):
    """The lookups underneath, driven directly with two boards.

    Every cache here used to be a single slot, which was correct while there was
    one board and silently wrong the moment there were two: the second board's
    question answered from the first board's reply, with nothing to distinguish
    that from a right answer. The service is configured with ids and does not
    resolve titles, so these are asserted on the client itself rather than
    through a tool that no longer asks.
    """

    def client(self, transport) -> VikunjaClient:
        return VikunjaClient("http://127.0.0.1:3456/api/v1", TOKEN, transport=transport)

    def test_resolving_one_title_does_not_answer_for_another(self):
        client = self.client(FakeVikunja())
        self.assertEqual(client.project_id(PROJECT_TITLE), PROJECT_ID)
        self.assertEqual(client.project_id(TRADER_TITLE), TRADER_PROJECT_ID)
        # And the other way round, in case the first answer is the one kept.
        fresh = self.client(FakeVikunja())
        self.assertEqual(fresh.project_id(TRADER_TITLE), TRADER_PROJECT_ID)
        self.assertEqual(fresh.project_id(PROJECT_TITLE), PROJECT_ID)

    def test_a_title_that_is_on_no_board_is_still_an_error(self):
        client = self.client(FakeVikunja())
        self.assertEqual(client.project_id(PROJECT_TITLE), PROJECT_ID)
        with self.assertRaises(VikunjaError):
            client.project_id("A board nobody has")

    def test_each_boards_kanban_view_is_its_own(self):
        client = self.client(FakeVikunja())
        self.assertEqual(client.kanban_view_id(PROJECT_ID), VIEW_ID)
        self.assertEqual(client.kanban_view_id(TRADER_PROJECT_ID), TRADER_VIEW_ID)

    def test_each_boards_title_is_its_own(self):
        client = self.client(FakeVikunja())
        self.assertEqual(client.project_title(PROJECT_ID), PROJECT_TITLE)
        self.assertEqual(client.project_title(TRADER_PROJECT_ID), TRADER_TITLE)

    def test_a_board_is_fetched_once_however_much_is_asked_of_it(self):
        vikunja = FakeVikunja()
        client = self.client(vikunja)
        for _ in range(3):
            client.project_title(TRADER_PROJECT_ID)
            client.kanban_view_id(TRADER_PROJECT_ID)
        self.assertEqual(
            [path for _, path, _ in vikunja.calls],
            [f"/projects/{TRADER_PROJECT_ID}"],
        )

    def test_an_empty_reply_is_not_remembered_as_the_board(self):
        """A Vikunja that answers nothing is broken now, not for the process's life."""
        real = FakeVikunja()
        answers = [None]

        def flaky(method, path, body=None):
            if answers and path == f"/projects/{TRADER_PROJECT_ID}":
                answers.pop()
                return None
            return real(method, path, body)

        client = self.client(flaky)
        with self.assertRaises(VikunjaError):
            client.project_title(TRADER_PROJECT_ID)
        self.assertEqual(client.project_title(TRADER_PROJECT_ID), TRADER_TITLE)


class TestWhatTheToolsPublish(McpTestCase):
    """A model can only pick a board it was told the id of."""

    def schema(self, name: str) -> dict:
        return {tool.name: tool for tool in self.service.tools()}[name].input_schema

    def test_every_task_tool_publishes_the_approved_ids(self):
        for name in ("get_task", "list_open_tasks", "search_tasks",
                     "update_task", "add_task_comment", "create_task"):
            with self.subTest(tool=name):
                described = self.schema(name)["properties"]["project_id"]["description"]
                self.assertIn(str(PROJECT_ID), described)
                self.assertIn(str(TRADER_PROJECT_ID), described)
                self.assertIn(TRADER_TITLE, described)

    def test_no_tool_still_claims_to_be_the_engine_board_alone(self):
        """The naming rule: a description that named one board would be wrong now."""
        for tool in self.service.tools():
            if tool.name in ("get_task", "list_open_tasks", "search_tasks",
                             "update_task", "add_task_comment", "create_task"):
                with self.subTest(tool=tool.name):
                    self.assertIn(TRADER_TITLE, tool.description)

    def test_the_selector_is_an_integer_and_the_schema_stays_closed(self):
        for name in ("get_task", "list_open_tasks", "search_tasks",
                     "update_task", "add_task_comment", "create_task"):
            with self.subTest(tool=name):
                schema = self.schema(name)
                self.assertEqual(schema["properties"]["project_id"]["type"], "integer")
                self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
