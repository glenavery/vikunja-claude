"""vkctl addresses a task the way the board does (task 660).

The connector stopped publishing Vikunja's immutable row id, and that left
``vkctl`` as the last place a session needed one: it took ``--task <id>`` or
``--ticket <NN>`` (the legacy title *prefix*) and had no way to say "the task
the board shows as #658". So closing a ticket meant reading a row id off a
``/tasks/<id>`` URL — the exact habit that put "task 659" in a branch name for
the ticket the board shows as #658.

``--number`` closes that. The other two selectors stay, because both answer a
question a human still asks — "I have this task URL open" and "this board
still carries the old prefix" — but neither is the ticket's identity, and the
usage text now leads with the one that is.

Driven through ``vkctl.main`` rather than through the client it calls: a test
that exercised the resolver directly would pass with the wiring deleted.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from tests.test_comment_reads import CliTestCase

#: The board's #9 is row 10, and row 9 is a different ticket sitting at #8.
TRAP_NUMBER = 9
TRAP_ROW_ID = 10
DECOY_ROW_ID = 9


class TestTheNumberSelector(CliTestCase):
    def test_a_number_reaches_the_task_the_board_shows(self):
        code, out = self.run_vkctl("show", "--number", str(TRAP_NUMBER))
        self.assertEqual(code, 0)
        self.assertIn("OpenClaw", out)

    def test_the_number_is_not_read_as_a_row_id(self):
        """Guards the test above: row 9 exists and is a different ticket.

        Without this, "--number 9 returned a ticket" would be satisfied by the
        old reading too — which is the failure mode this whole scheme exists
        for, because it returns a real task and reports success.
        """
        by_number, _ = self.run_vkctl("show", "--number", str(TRAP_NUMBER))
        by_row, out_row = self.run_vkctl("show", "--task", str(DECOY_ROW_ID))
        self.assertEqual(by_row, 0)
        self.assertNotIn("OpenClaw", out_row)

    def test_show_reports_the_board_reference_not_the_row_id(self):
        """What the command prints is what a reader will quote back."""
        _, out = self.run_vkctl("show", "--number", str(TRAP_NUMBER))
        self.assertIn(f"board: #{TRAP_NUMBER}", out)
        self.assertNotIn(f"task id: {TRAP_ROW_ID}", out)

    def test_the_three_selectors_stay_mutually_exclusive(self):
        """Two identities in one invocation is a refusal, not a precedence rule."""
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()):
                self.run_vkctl("show", "--number", "9", "--task", "10")

    def test_a_number_no_task_carries_is_refused_not_retried_as_a_row_id(self):
        """The refusal task 649 made structural, now reachable from the CLI.

        A fallback here would usually succeed, and succeeding is the damage.
        """
        code, out = self.run_vkctl("show", "--number", "99999")
        self.assertNotEqual(code, 0, "a number with no task must not report success")
        self.assertEqual(out.strip(), "", "nothing may be printed for a refusal")


class TestTheUsageLeadsWithTheIdentity(unittest.TestCase):
    def test_the_docstring_shows_number_not_a_row_id(self):
        import vkctl

        doc = vkctl.__doc__ or ""
        self.assertIn("--number", doc)
        self.assertIn("vkctl.py close   --number", doc)
        # The row id is still documented, but as one of the other ways in.
        self.assertIn("neither is the ticket's identity", doc)


if __name__ == "__main__":
    unittest.main()
