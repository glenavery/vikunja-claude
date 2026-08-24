"""Closing, reopening and moving a ticket from the connector (task 669).

The MCP could file work and comment on it but not finish it: `update_task`
says in its own contract that it cannot change status or bucket, and nothing
else covered them. So the only way to close a ticket was shell access to
`vkctl` on the host, and a ticket closed as the last step of finishing it
could not be reopened for review.

ONE CONTROL, OVER COLUMNS. Vikunja couples `done` to the board's done-bucket:
a task moved into Done is marked done and one moved out of it is reopened.
Measured on the live board before this was designed — `vkctl move` out of Done
left `done` False, and `vkctl close` left the task sitting in Done without
naming the column. Publishing a bucket knob beside a done knob would let a
caller set them against each other, which is the shape these boards spent a
week removing from their reports.

The coupling is verified rather than trusted: every move is read back, and a
task whose flag disagrees with its column is an error, not a success.
"""

from __future__ import annotations

import unittest

from vikunja_claude.mcp_service import (
    CHANGE_COMMENT, CHANGE_STATUS, CHANGE_UPDATE, ToolError,
)

from .fakes import PROJECT_ID  # noqa: F401  (board identity, used by support)
from .support import McpTestCase

#: The fixture's board number for a task sitting in Ready, and one in Backlog.
READY = 8
BACKLOG = 4


class TestThePreviewChangesNothing(McpTestCase):
    def test_it_names_the_current_and_proposed_column_and_writes_nothing(self):
        before = self.service.get_task(READY)
        previewed = self.service.set_task_status(task_number=READY, bucket="Done")

        self.assertFalse(previewed["changed"])
        self.assertTrue(previewed["approval_required"])
        self.assertEqual(previewed["current"], {"bucket": "Ready", "done": False})
        self.assertEqual(previewed["proposed"], {"bucket": "Done", "done": True})
        self.assertTrue(previewed["approval_token"])
        # Nothing moved.
        self.assertEqual(self.service.get_task(READY)["bucket"], before["bucket"])
        self.assertEqual(self.service.get_task(READY)["status"], before["status"])

    def test_a_column_the_board_does_not_have_is_refused_with_the_real_ones(self):
        """Refused, not guessed at. "In Progress" and a hypothetical "In
        Review" would both answer to a prefix match, and picking one would be
        guessing at a write."""
        with self.assertRaises(ToolError) as caught:
            self.service.set_task_status(task_number=READY, bucket="Reddy")
        message = str(caught.exception)
        self.assertIn("Reddy", message)
        self.assertIn("Nothing was changed", message)
        for column in ("Backlog", "Ready", "In Progress", "Waiting", "Done"):
            self.assertIn(column, message)

    def test_a_blank_column_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.service.set_task_status(task_number=READY, bucket="  ")
        self.assertIn("Nothing was changed", str(caught.exception))

    def test_the_column_is_matched_case_insensitively(self):
        previewed = self.service.set_task_status(task_number=READY, bucket="dOnE")
        self.assertEqual(previewed["proposed"]["bucket"], "Done")

    def test_moving_to_the_column_it_is_already_in_is_a_stated_no_op(self):
        result = self.service.set_task_status(task_number=READY, bucket="Ready")
        self.assertFalse(result["changed"])
        self.assertNotIn("approval_token", result)
        self.assertIn("already", result["reason"])


class TestAnApprovedMove(McpTestCase):
    def _approved(self, number: int, bucket: str) -> dict:
        preview = self.service.set_task_status(task_number=number, bucket=bucket)
        return self.service.set_task_status(
            task_number=number, bucket=bucket,
            approval_token=preview["approval_token"])

    def test_moving_to_done_closes_the_ticket(self):
        result = self._approved(READY, "Done")
        self.assertTrue(result["changed"])
        self.assertEqual(result["bucket"], "Done")
        self.assertTrue(result["done"])
        self.assertFalse(result["reopened"])
        # Read back through the service, not from the return value.
        self.assertEqual(self.service.get_task(READY)["status"], "done")

    def test_moving_out_of_done_reopens_it(self):
        self._approved(READY, "Done")
        self.assertEqual(self.service.get_task(READY)["status"], "done")

        result = self._approved(READY, "In Progress")
        self.assertTrue(result["changed"])
        self.assertFalse(result["done"])
        self.assertTrue(result["reopened"], "the answer says a close was undone")
        self.assertEqual(self.service.get_task(READY)["status"], "open")

    def test_a_move_between_open_columns_closes_nothing(self):
        result = self._approved(BACKLOG, "In Progress")
        self.assertEqual(result["bucket"], "In Progress")
        self.assertFalse(result["done"])
        self.assertFalse(result["reopened"])

    def test_the_move_is_recorded_in_the_mutation_ledger(self):
        self._approved(READY, "Done")
        entries = [
            line for line in
            self.config.mutation_ledger_path.read_text().splitlines() if line
        ]
        self.assertTrue(any(CHANGE_STATUS in line for line in entries))
        self.assertTrue(any('"to_bucket": "Done"' in line for line in entries))


class TestTheApprovalIsBoundToThisMove(McpTestCase):
    def test_a_status_token_cannot_be_redeemed_as_a_comment_or_an_edit(self):
        """The kinds are kept apart so one approval cannot spend another."""
        preview = self.service.set_task_status(task_number=READY, bucket="Done")
        token = preview["approval_token"]
        with self.assertRaises(ToolError):
            self.service.add_task_comment(
                task_number=READY, comment="hello", approval_token=token)
        with self.assertRaises(ToolError):
            self.service.update_task(
                task_number=READY, title="Renamed", approval_token=token)
        self.assertEqual(self.service.get_task(READY)["bucket"], "Ready")

    def test_a_token_for_one_column_cannot_move_the_task_to_another(self):
        preview = self.service.set_task_status(task_number=READY, bucket="Done")
        with self.assertRaises(ToolError):
            self.service.set_task_status(
                task_number=READY, bucket="Waiting",
                approval_token=preview["approval_token"])
        self.assertEqual(self.service.get_task(READY)["bucket"], "Ready")

    def test_the_three_change_kinds_are_distinct(self):
        self.assertEqual(
            len({CHANGE_UPDATE, CHANGE_COMMENT, CHANGE_STATUS}), 3)


class TestTheMoveIsReadBack(McpTestCase):
    """The call not raising is not evidence the task went where it was sent."""

    def test_a_task_that_landed_in_another_column_is_an_error(self):
        self.vikunja.misroute_moves_to = "Waiting"
        preview = self.service.set_task_status(task_number=READY, bucket="Done")
        with self.assertRaises(ToolError) as caught:
            self.service.set_task_status(
                task_number=READY, bucket="Done",
                approval_token=preview["approval_token"])
        message = str(caught.exception)
        self.assertIn("Waiting", message)
        self.assertIn("Read the board before acting on this", message)

    def test_the_same_move_succeeds_when_it_lands_where_it_was_sent(self):
        """The control, so the test above cannot pass against a tool that
        always raises."""
        preview = self.service.set_task_status(task_number=READY, bucket="Done")
        result = self.service.set_task_status(
            task_number=READY, bucket="Done",
            approval_token=preview["approval_token"])
        self.assertEqual(result["bucket"], "Done")


class TestThroughTheProtocol(McpTestCase):
    """Driven through `tools/call`, so the tool's own argument handling runs.

    Every other test here calls the service directly, which never executes the
    `run` lambda in the tool declaration. That gap shipped a `_required_int`
    that does not exist: 743 tests passed and pyright found it. A tool nothing
    ever calls through the protocol is a tool with an untested front door.
    """

    def _call(self, **arguments) -> dict:
        from .test_mcp_protocol import request

        response = self.protocol.handle(
            request("tools/call",
                    {"name": "set_task_status", "arguments": arguments}))
        return response["result"]

    def test_a_preview_and_an_approved_move_both_go_through(self):
        preview = self._call(task_number=READY, bucket="Done")
        content = preview["structuredContent"]
        self.assertTrue(content["approval_required"])

        applied = self._call(task_number=READY, bucket="Done",
                             approval_token=content["approval_token"])
        self.assertTrue(applied["structuredContent"]["changed"])
        self.assertTrue(applied["structuredContent"]["done"])

    def test_a_missing_task_number_is_refused_by_the_protocol(self):
        """A JSON-RPC error, not a Python traceback escaping the handler."""
        from .test_mcp_protocol import request

        response = self.protocol.handle(
            request("tools/call",
                    {"name": "set_task_status", "arguments": {"bucket": "Done"}}))
        self.assertIn("error", response)
        self.assertNotIn("result", response)
        # And nothing moved.
        self.assertEqual(self.service.get_task(READY)["bucket"], "Ready")

    def test_list_recently_done_goes_through_too(self):
        from .test_mcp_protocol import request

        result = self.protocol.handle(
            request("tools/call",
                    {"name": "list_recently_done", "arguments": {"limit": 5}}))["result"]
        self.assertIn("tasks", result["structuredContent"])


class TestTheCouplingIsCheckedNotTrusted(McpTestCase):
    """A Vikunja whose done-bucket is not configured accepts the move and
    leaves the ticket open. The tool must say so, not report a close."""

    def setUp(self) -> None:
        super().setUp()
        # The column still exists and the move still succeeds; only the flag
        # stops following it. That is exactly the half-success the read-back
        # exists to catch, and it cannot be reached by removing the column —
        # that is refused earlier, by name.
        self.vikunja.couple_done = False

    def test_a_move_that_does_not_close_is_an_error_not_a_success(self):
        preview = self.service.set_task_status(task_number=READY, bucket="Done")
        with self.assertRaises(ToolError) as caught:
            self.service.set_task_status(
                task_number=READY, bucket="Done",
                approval_token=preview["approval_token"])
        message = str(caught.exception)
        self.assertIn("done flag", message)
        self.assertIn("the column was changed, the closed state was not", message)

    def test_the_column_move_itself_still_happened_and_is_reported_as_such(self):
        """The error describes what is true: the task did move. Saying nothing
        happened would send a reader looking for a ticket that is no longer
        where they left it."""
        preview = self.service.set_task_status(task_number=READY, bucket="Done")
        with self.assertRaises(ToolError):
            self.service.set_task_status(
                task_number=READY, bucket="Done",
                approval_token=preview["approval_token"])
        self.assertEqual(self.service.get_task(READY)["bucket"], "Done")
        self.assertEqual(self.service.get_task(READY)["status"], "open")

    def test_with_the_coupling_present_the_same_move_succeeds(self):
        """The control: without this, the two tests above would pass against a
        tool that always raised."""
        self.vikunja.couple_done = True
        preview = self.service.set_task_status(task_number=READY, bucket="Done")
        result = self.service.set_task_status(
            task_number=READY, bucket="Done",
            approval_token=preview["approval_token"])
        self.assertTrue(result["done"])


if __name__ == "__main__":
    unittest.main()
