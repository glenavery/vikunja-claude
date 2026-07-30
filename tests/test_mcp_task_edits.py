"""Editing a task and commenting on one: what it takes, and what is refused.

Task 196 moved the write boundary. These tests are what holds it where it moved
to, so most of them assert on *the calls the fake Vikunja saw* rather than only
on return values — a refusal that returned the right message and still issued
the write would satisfy a return-value check and fail these.

The approval flow is two calls. The first writes nothing and hands back the
exact current value, the exact proposal and a token for that one change; the
second must carry that token, the same text, and a task that has not moved. Note
what that is and is not: no server can see the conversation, so this cannot
prove a human said yes. It proves no edit happens without a round trip that put
the before and after in front of the client, and that what is written is
byte-for-byte what that round trip described.
"""

from __future__ import annotations

import json
import unittest
from copy import deepcopy

from vikunja_claude.html_text import html_to_text
from vikunja_claude.mcp import ToolError
from vikunja_claude.mcp_service import (
    MAX_COMMENT_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_PENDING_APPROVALS,
    MAX_TITLE_CHARS,
    McpService,
)
from vikunja_claude.vikunja import VikunjaClient

from .fakes import PROJECT_ID, FakeVikunja, task
from .support import McpTestCase

#: On the board, in Ready, with a description made of real markup.
TASK = 9
CURRENT_TITLE = "#33 Back up Vikunja database"
CURRENT_TEXT = "Vikunja is now the authoritative queue.\n\n- cover `vikunja-db`"

NEW_TITLE = "#33 Back up the Vikunja database nightly"
NEW_TEXT = "Vikunja is the authoritative queue.\n\nSo it needs a nightly dump."

#: A task that exists in Vikunja but on somebody else's board.
FOREIGN = 777


def signatures(vikunja: FakeVikunja) -> list[str]:
    return [f"{method} {path}" for method, path, _ in vikunja.calls]


class EditTestCase(McpTestCase):
    """An McpService whose Vikunja also serves one task from another project."""

    def setUp(self) -> None:
        super().setUp()
        self.vikunja = FakeVikunja(
            layout=self.layout,
            comments=deepcopy(self.comments or {}),
            foreign=[task(FOREIGN, "Someone else's ticket", "2026-07-01T00:00:00Z")],
        )
        self.client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.service = McpService(self.config, self.client)
        from vikunja_claude.mcp import McpProtocol

        self.protocol = McpProtocol(self.service.tools())

    # -- helpers -----------------------------------------------------------

    def stored(self, task_id: int = TASK) -> dict:
        return self.vikunja._find(task_id)

    def wrote_to_task(self) -> list[str]:
        return [s for s in signatures(self.vikunja) if s.startswith("POST /tasks/")]

    def assertWroteNothing(self) -> None:
        self.assertEqual(
            [s for s in signatures(self.vikunja) if not s.startswith("GET ")],
            [],
            "the boundary issued a write on a path that must not write",
        )

    def preview(self, **arguments) -> dict:
        return self.service.update_task(**arguments)

    def approved_update(self, **arguments) -> dict:
        """Preview, then commit with the token the preview handed back."""
        previewed = self.service.update_task(**arguments)
        return self.service.update_task(
            **arguments, approval_token=previewed["approval_token"]
        )


class TestThePreviewChangesNothing(EditTestCase):
    def test_a_call_without_a_token_returns_the_exact_current_and_proposed(self):
        previewed = self.preview(task_id=TASK, title=NEW_TITLE, description=NEW_TEXT)

        self.assertFalse(previewed["applied"])
        self.assertTrue(previewed["approval_required"])
        self.assertEqual(previewed["current"]["title"], CURRENT_TITLE)
        self.assertEqual(previewed["current"]["description"], CURRENT_TEXT)
        self.assertEqual(previewed["proposed"]["title"], NEW_TITLE)
        self.assertEqual(previewed["proposed"]["description"], NEW_TEXT)
        self.assertEqual(previewed["changed_fields"], ["title", "description"])
        self.assertEqual(previewed["url"], f"http://127.0.0.1:3456/tasks/{TASK}")

    def test_the_preview_reads_and_does_not_write(self):
        self.preview(task_id=TASK, title=NEW_TITLE)
        self.assertWroteNothing()
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)

    def test_the_current_value_is_the_text_a_human_reads_not_the_markup(self):
        """The user approves what they were shown, so it is shown as text."""
        previewed = self.preview(task_id=TASK, title=NEW_TITLE)
        self.assertNotIn("<strong>", previewed["current"]["description"])
        self.assertIn("authoritative", previewed["current"]["description"])

    def test_previewing_the_same_change_twice_issues_one_approval(self):
        first = self.preview(task_id=TASK, title=NEW_TITLE)
        second = self.preview(task_id=TASK, title=NEW_TITLE)
        self.assertEqual(first["approval_token"], second["approval_token"])

    def test_previewing_a_different_change_issues_a_different_approval(self):
        first = self.preview(task_id=TASK, title=NEW_TITLE)
        second = self.preview(task_id=TASK, title=NEW_TITLE + " again")
        self.assertNotEqual(first["approval_token"], second["approval_token"])

    def test_an_abandoned_preview_does_not_grow_without_bound(self):
        for index in range(MAX_PENDING_APPROVALS + 5):
            self.preview(task_id=TASK, title=f"#33 Title {index}")
        self.assertLessEqual(len(self.service._pending), MAX_PENDING_APPROVALS)


class TestAnApprovedUpdateIsApplied(EditTestCase):
    def test_the_title_alone_can_be_replaced(self):
        result = self.approved_update(task_id=TASK, title=NEW_TITLE)

        self.assertTrue(result["applied"])
        self.assertEqual(result["changed_fields"], ["title"])
        self.assertEqual(result["task_id"], TASK)
        self.assertEqual(result["title"], NEW_TITLE)
        self.assertEqual(result["url"], f"http://127.0.0.1:3456/tasks/{TASK}")
        self.assertEqual(self.stored()["title"], NEW_TITLE)

    def test_replacing_the_title_keeps_the_description(self):
        """The write is a whole-task replace; this is the field it must carry."""
        self.approved_update(task_id=TASK, title=NEW_TITLE)
        self.assertEqual(html_to_text(self.stored()["description"]), CURRENT_TEXT)

    def test_the_description_alone_can_be_replaced(self):
        result = self.approved_update(task_id=TASK, description=NEW_TEXT)

        self.assertEqual(result["changed_fields"], ["description"])
        self.assertEqual(html_to_text(self.stored()["description"]), NEW_TEXT)
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)

    def test_both_fields_can_be_replaced_in_one_write(self):
        result = self.approved_update(task_id=TASK, title=NEW_TITLE, description=NEW_TEXT)

        self.assertEqual(result["changed_fields"], ["title", "description"])
        self.assertEqual(self.stored()["title"], NEW_TITLE)
        self.assertEqual(html_to_text(self.stored()["description"]), NEW_TEXT)
        self.assertEqual(len(self.wrote_to_task()), 1, self.wrote_to_task())

    def test_the_description_is_escaped_never_interpreted(self):
        self.approved_update(task_id=TASK, description="Watch <script>alert(1)</script>")
        stored = self.stored()["description"]
        self.assertNotIn("<script>", stored)
        self.assertIn("&lt;script&gt;", stored)

    def test_it_changes_nothing_but_the_two_fields(self):
        before = deepcopy(self.stored())
        self.approved_update(task_id=TASK, title=NEW_TITLE, description=NEW_TEXT)
        after = self.stored()

        for field in ("id", "done", "priority", "labels", "created"):
            with self.subTest(field=field):
                self.assertEqual(after[field], before[field])

    def test_it_never_moves_the_task_between_columns(self):
        self.approved_update(task_id=TASK, title=NEW_TITLE)
        self.assertEqual(self.vikunja.bucket_of(TASK), "Ready")
        for signature in signatures(self.vikunja):
            self.assertNotIn("/buckets/", signature)

    def test_the_write_is_a_read_modify_write_of_the_whole_task(self):
        self.approved_update(task_id=TASK, title=NEW_TITLE)
        calls = [c for c in self.vikunja.calls if c[1] == f"/tasks/{TASK}"]
        self.assertEqual([c[0] for c in calls], ["GET", "POST"])
        posted = calls[1][2]
        self.assertTrue(posted["description"], "the replace body dropped the description")

    def test_the_replaced_value_is_recorded_before_it_is_gone(self):
        self.approved_update(task_id=TASK, title=NEW_TITLE, description=NEW_TEXT)

        lines = self.config.mutation_ledger_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["kind"], "update")
        self.assertEqual(record["task_id"], TASK)
        self.assertEqual(record["project_id"], PROJECT_ID)
        self.assertEqual(record["changed_fields"], ["title", "description"])
        self.assertEqual(record["replaced"]["title"], CURRENT_TITLE)
        self.assertEqual(record["replaced"]["description"], CURRENT_TEXT)
        self.assertEqual(record["stored"]["title"], NEW_TITLE)
        self.assertTrue(record["at"])

    def test_the_creation_ledger_is_left_alone(self):
        """An edit is not a creation, and must not read as one."""
        self.approved_update(task_id=TASK, title=NEW_TITLE)
        self.assertFalse(self.config.ledger_path.exists())


class TestAnUpdateWithoutApprovalIsNotApplied(EditTestCase):
    def test_one_call_can_never_be_enough(self):
        self.preview(task_id=TASK, title=NEW_TITLE, description=NEW_TEXT)
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)
        self.assertWroteNothing()

    def test_an_invented_token_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(
                task_id=TASK, title=NEW_TITLE, approval_token="not-a-real-token"
            )
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)
        self.assertWroteNothing()

    def test_an_approval_cannot_be_spent_twice(self):
        """Single use, so a replayed call cannot re-apply a reverted edit."""
        previewed = self.preview(task_id=TASK, title=NEW_TITLE)
        token = previewed["approval_token"]
        self.service.update_task(task_id=TASK, title=NEW_TITLE, approval_token=token)

        # Put the old title back the way a person would, then replay the call.
        self.stored()["title"] = CURRENT_TITLE
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(task_id=TASK, title=NEW_TITLE, approval_token=token)
        self.assertIn("already have been used", str(caught.exception))
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)

    def test_an_approval_for_one_task_cannot_be_used_on_another(self):
        previewed = self.preview(task_id=TASK, title=NEW_TITLE)
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(
                task_id=10, title=NEW_TITLE, approval_token=previewed["approval_token"]
            )
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(self.stored(10)["title"], "#34 Version the OpenClaw health-check skill")

    def test_approving_one_text_does_not_approve_another(self):
        """The whole point: the token binds the exact text that was shown."""
        previewed = self.preview(task_id=TASK, title=NEW_TITLE)
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(
                task_id=TASK,
                title="#33 Something the user never saw",
                approval_token=previewed["approval_token"],
            )
        self.assertIn("not the change that was approved", str(caught.exception))
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)
        self.assertWroteNothing()

    def test_adding_a_second_field_after_approval_is_refused(self):
        previewed = self.preview(task_id=TASK, title=NEW_TITLE)
        with self.assertRaises(ToolError):
            self.service.update_task(
                task_id=TASK,
                title=NEW_TITLE,
                description=NEW_TEXT,
                approval_token=previewed["approval_token"],
            )
        self.assertEqual(html_to_text(self.stored()["description"]), CURRENT_TEXT)
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)

    def test_a_task_that_moved_under_the_approval_is_refused(self):
        """The value the user was shown is no longer the value on the board."""
        previewed = self.preview(task_id=TASK, description=NEW_TEXT)
        self.stored()["description"] = "<p>Someone else edited this meanwhile.</p>"

        with self.assertRaises(ToolError) as caught:
            self.service.update_task(
                task_id=TASK,
                description=NEW_TEXT,
                approval_token=previewed["approval_token"],
            )
        message = str(caught.exception)
        self.assertIn("has changed since", message)
        self.assertIn("Nothing was changed", message)
        self.assertEqual(
            self.stored()["description"], "<p>Someone else edited this meanwhile.</p>"
        )

    def test_a_refused_approval_is_not_left_usable(self):
        previewed = self.preview(task_id=TASK, title=NEW_TITLE)
        token = previewed["approval_token"]
        with self.assertRaises(ToolError):
            self.service.update_task(task_id=TASK, title="#33 Other", approval_token=token)

        with self.assertRaises(ToolError):
            self.service.update_task(task_id=TASK, title=NEW_TITLE, approval_token=token)
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)


class TestUpdateRefusals(EditTestCase):
    def test_a_task_on_another_board_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(task_id=FOREIGN, title="Renamed by the connector")
        message = str(caught.exception)
        self.assertIn("AI Alpha Engine", message)
        self.assertIn("Nothing was changed", message)
        self.assertWroteNothing()

    def test_a_task_on_another_board_is_refused_even_with_an_approval(self):
        """The refusal is the lookup, not the approval — so it cannot be bought."""
        previewed = self.preview(task_id=TASK, title=NEW_TITLE)
        with self.assertRaises(ToolError):
            self.service.update_task(
                task_id=FOREIGN,
                title=NEW_TITLE,
                approval_token=previewed["approval_token"],
            )
        self.assertEqual(self.vikunja.foreign[FOREIGN]["title"], "Someone else's ticket")

    def test_the_foreign_task_really_is_reachable_in_vikunja(self):
        """Guards the two tests above: without this they prove only that 777
        does not exist anywhere."""
        self.assertEqual(
            self.client.call("GET", f"/tasks/{FOREIGN}")["title"],
            "Someone else's ticket",
        )

    def test_a_task_id_that_exists_nowhere_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(task_id=4242, title="Renamed")
        self.assertIn("4242", str(caught.exception))
        self.assertWroteNothing()

    def test_naming_no_field_at_all_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.service.update_task(task_id=TASK)
        self.assertIn("complete replacement title", str(caught.exception))
        self.assertWroteNothing()

    def test_a_blank_title_is_refused(self):
        with self.assertRaises(ToolError):
            self.service.update_task(task_id=TASK, title="   ")
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)

    def test_a_blank_description_is_refused(self):
        with self.assertRaises(ToolError):
            self.service.update_task(task_id=TASK, description="  \n ")
        self.assertEqual(html_to_text(self.stored()["description"]), CURRENT_TEXT)

    def test_an_oversized_title_is_refused(self):
        with self.assertRaises(ToolError):
            self.service.update_task(task_id=TASK, title="x" * (MAX_TITLE_CHARS + 1))
        self.assertWroteNothing()

    def test_an_oversized_description_is_refused(self):
        with self.assertRaises(ToolError):
            self.service.update_task(
                task_id=TASK, description="x" * (MAX_DESCRIPTION_CHARS + 1)
            )
        self.assertWroteNothing()


class TestUpdateIsIdempotent(EditTestCase):
    def test_asking_for_the_value_the_task_already_holds_is_a_no_op(self):
        result = self.service.update_task(task_id=TASK, title=CURRENT_TITLE)

        self.assertFalse(result["applied"])
        self.assertEqual(result["changed_fields"], [])
        self.assertNotIn("approval_token", result)
        self.assertWroteNothing()

    def test_a_no_op_is_reported_rather_than_refused(self):
        result = self.service.update_task(
            task_id=TASK, title=CURRENT_TITLE, description=CURRENT_TEXT
        )
        self.assertIn("nothing to change", result["reason"])
        self.assertEqual(result["task_id"], TASK)

    def test_repeating_an_applied_change_writes_nothing_a_second_time(self):
        self.approved_update(task_id=TASK, title=NEW_TITLE, description=NEW_TEXT)
        writes = len(self.wrote_to_task())

        repeated = self.service.update_task(
            task_id=TASK, title=NEW_TITLE, description=NEW_TEXT
        )
        self.assertFalse(repeated["applied"])
        self.assertEqual(repeated["changed_fields"], [])
        self.assertEqual(len(self.wrote_to_task()), writes)

    def test_one_of_two_fields_already_matching_narrows_the_change(self):
        result = self.preview(task_id=TASK, title=CURRENT_TITLE, description=NEW_TEXT)
        self.assertEqual(result["changed_fields"], ["description"])

    def test_whitespace_around_the_same_value_is_still_a_no_op(self):
        result = self.service.update_task(task_id=TASK, title=f"  {CURRENT_TITLE}  ")
        self.assertEqual(result["changed_fields"], [])


class TestComments(EditTestCase):
    TEXT = "Merged as 4214664. The stage list now names its own outcome."

    def approved_comment(self, **arguments) -> dict:
        previewed = self.service.add_task_comment(**arguments)
        return self.service.add_task_comment(
            **arguments, approval_token=previewed["approval_token"]
        )

    def comments_on(self, task_id: int = TASK) -> list[dict]:
        return self.vikunja.comments.get(task_id, [])

    def test_the_preview_writes_nothing_and_names_the_target(self):
        previewed = self.service.add_task_comment(task_id=TASK, comment=self.TEXT)

        self.assertFalse(previewed["added"])
        self.assertTrue(previewed["approval_required"])
        self.assertEqual(previewed["comment"], self.TEXT)
        self.assertEqual(previewed["task_id"], TASK)
        self.assertEqual(previewed["title"], CURRENT_TITLE)
        self.assertEqual(previewed["url"], f"http://127.0.0.1:3456/tasks/{TASK}")
        self.assertEqual(self.comments_on(), [])
        self.assertWroteNothing()

    def test_an_approved_comment_is_appended(self):
        result = self.approved_comment(task_id=TASK, comment=self.TEXT)

        self.assertTrue(result["added"])
        self.assertEqual(result["task_id"], TASK)
        self.assertIsNotNone(result["comment_id"])
        self.assertEqual(result["url"], f"http://127.0.0.1:3456/tasks/{TASK}")
        self.assertEqual(len(self.comments_on()), 1)
        self.assertEqual(html_to_text(self.comments_on()[0]["comment"]), self.TEXT)

    def test_the_comment_comes_back_through_get_task(self):
        self.approved_comment(task_id=TASK, comment=self.TEXT)
        read = self.service.get_task(TASK)
        self.assertEqual([c["text"] for c in read["comments"]], [self.TEXT])

    def test_the_comment_is_escaped_never_interpreted(self):
        self.approved_comment(task_id=TASK, comment="Careful: <script>alert(1)</script>")
        stored = self.comments_on()[0]["comment"]
        self.assertNotIn("<script>", stored)
        self.assertIn("&lt;script&gt;", stored)

    def test_commenting_changes_nothing_on_the_task_itself(self):
        before = deepcopy(self.stored())
        self.approved_comment(task_id=TASK, comment=self.TEXT)

        self.assertEqual(self.stored(), before)
        self.assertEqual(self.wrote_to_task(), [])
        self.assertEqual(self.vikunja.bucket_of(TASK), "Ready")

    def test_the_comment_is_recorded(self):
        result = self.approved_comment(task_id=TASK, comment=self.TEXT)

        lines = self.config.mutation_ledger_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["kind"], "comment")
        self.assertEqual(record["task_id"], TASK)
        self.assertEqual(record["comment_id"], result["comment_id"])
        self.assertEqual(record["comment"], self.TEXT)

    def test_a_comment_without_approval_is_not_written(self):
        self.service.add_task_comment(task_id=TASK, comment=self.TEXT)
        self.assertEqual(self.comments_on(), [])

    def test_an_invented_token_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.service.add_task_comment(
                task_id=TASK, comment=self.TEXT, approval_token="nope"
            )
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(self.comments_on(), [])

    def test_approving_one_comment_does_not_approve_another(self):
        previewed = self.service.add_task_comment(task_id=TASK, comment=self.TEXT)
        with self.assertRaises(ToolError) as caught:
            self.service.add_task_comment(
                task_id=TASK,
                comment="Something else entirely.",
                approval_token=previewed["approval_token"],
            )
        self.assertIn("not the change that was approved", str(caught.exception))
        self.assertEqual(self.comments_on(), [])

    def test_an_update_approval_cannot_be_redeemed_as_a_comment(self):
        """The two kinds are separate, so one approval buys one operation."""
        previewed = self.service.update_task(task_id=TASK, title=NEW_TITLE)
        with self.assertRaises(ToolError):
            self.service.add_task_comment(
                task_id=TASK,
                comment=self.TEXT,
                approval_token=previewed["approval_token"],
            )
        self.assertEqual(self.comments_on(), [])

    def test_the_same_comment_twice_is_written_once(self):
        first = self.approved_comment(task_id=TASK, comment=self.TEXT)
        # Not even offered an approval the second time: the duplicate is
        # recognised on the way in, so there is nothing to put to the user.
        second = self.service.add_task_comment(task_id=TASK, comment=self.TEXT)

        self.assertNotIn("approval_token", second)
        self.assertTrue(first["added"])
        self.assertFalse(second["added"])
        self.assertEqual(second["comment_id"], first["comment_id"])
        self.assertIn("already on the task", second["reason"])
        self.assertEqual(len(self.comments_on()), 1)

    def test_a_duplicate_is_recognised_across_a_restart(self):
        """The board is the record, not a ledger this process happens to hold."""
        self.approved_comment(task_id=TASK, comment=self.TEXT)

        restarted = McpService(self.config, self.client)
        result = restarted.add_task_comment(task_id=TASK, comment=self.TEXT)
        self.assertFalse(result["added"])
        self.assertEqual(len(self.comments_on()), 1)

    def test_a_duplicate_of_somebody_elses_comment_is_also_suppressed(self):
        self.vikunja.comments[TASK] = [
            {"id": 51, "comment": f"<p>{self.TEXT}</p>", "author": {"username": "glen"}}
        ]
        result = self.service.add_task_comment(task_id=TASK, comment=self.TEXT)

        self.assertFalse(result["added"])
        self.assertEqual(result["comment_id"], 51)

    def test_a_genuinely_different_comment_is_written(self):
        self.approved_comment(task_id=TASK, comment=self.TEXT)
        second = self.approved_comment(task_id=TASK, comment="A second, different note.")

        self.assertTrue(second["added"])
        self.assertEqual(len(self.comments_on()), 2)

    def test_a_task_on_another_board_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.service.add_task_comment(task_id=FOREIGN, comment=self.TEXT)
        self.assertIn("AI Alpha Engine", str(caught.exception))
        self.assertEqual(self.comments_on(FOREIGN), [])
        self.assertWroteNothing()

    def test_a_task_id_that_exists_nowhere_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.service.add_task_comment(task_id=4242, comment=self.TEXT)
        self.assertIn("4242", str(caught.exception))
        self.assertWroteNothing()

    def test_a_blank_comment_is_refused(self):
        with self.assertRaises(ToolError):
            self.service.add_task_comment(task_id=TASK, comment="   ")
        self.assertWroteNothing()

    def test_an_oversized_comment_is_refused(self):
        with self.assertRaises(ToolError):
            self.service.add_task_comment(
                task_id=TASK, comment="x" * (MAX_COMMENT_CHARS + 1)
            )
        self.assertWroteNothing()

    def test_nothing_here_can_edit_or_delete_an_existing_comment(self):
        self.approved_comment(task_id=TASK, comment=self.TEXT)
        for method, path, _ in self.vikunja.calls:
            if "/comments" in path:
                self.assertIn(method, ("GET", "PUT"), f"{method} {path}")


class TestThroughTheProtocol(EditTestCase):
    """The same guarantees, driven the way a client drives them."""

    def result(self, name: str, **arguments) -> dict:
        response = self.protocol.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return response["result"]

    def test_both_tools_are_advertised(self):
        listed = self.protocol.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )["result"]["tools"]
        names = {tool["name"] for tool in listed}
        self.assertIn("update_task", names)
        self.assertIn("add_task_comment", names)

    def test_both_tool_descriptions_state_the_approval_requirement(self):
        listed = {
            tool["name"]: tool["description"].lower()
            for tool in self.protocol.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )["result"]["tools"]
        }
        for name in ("update_task", "add_task_comment"):
            with self.subTest(tool=name):
                self.assertIn("approv", listed[name])
                self.assertIn("show the user", listed[name])

    def test_an_update_takes_two_calls_over_the_protocol(self):
        previewed = self.result("update_task", task_id=TASK, title=NEW_TITLE)[
            "structuredContent"
        ]
        self.assertFalse(previewed["applied"])
        self.assertEqual(self.stored()["title"], CURRENT_TITLE)

        applied = self.result(
            "update_task",
            task_id=TASK,
            title=NEW_TITLE,
            approval_token=previewed["approval_token"],
        )["structuredContent"]
        self.assertTrue(applied["applied"])
        self.assertEqual(self.stored()["title"], NEW_TITLE)

    def test_a_comment_takes_two_calls_over_the_protocol(self):
        previewed = self.result("add_task_comment", task_id=TASK, comment="Noted.")[
            "structuredContent"
        ]
        self.assertFalse(previewed["added"])
        self.assertEqual(self.vikunja.comments.get(TASK, []), [])

        added = self.result(
            "add_task_comment",
            task_id=TASK,
            comment="Noted.",
            approval_token=previewed["approval_token"],
        )["structuredContent"]
        self.assertTrue(added["added"])
        self.assertEqual(len(self.vikunja.comments[TASK]), 1)

    def test_a_refusal_is_a_visible_error_not_a_silent_success(self):
        result = self.result("update_task", task_id=FOREIGN, title="Renamed")
        self.assertTrue(result["isError"])
        self.assertIn("Nothing was changed", result["content"][0]["text"])

    def test_update_task_requires_a_task_id_before_the_tool_runs(self):
        response = self.protocol.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "update_task", "arguments": {"title": "x"}},
            }
        )
        self.assertIn("task_id", response["error"]["message"])
        self.assertEqual(self.vikunja.calls, [])


if __name__ == "__main__":
    unittest.main()
