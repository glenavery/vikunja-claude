"""vkctl addresses a task the way the board does (task 659).

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


class TestEveryLineItPrintsNamesTheBoardNumber(CliTestCase):
    """Not just the line a reviewer happened to look at.

    ``Ticket.reference`` falls back to ``task <row id>`` when a title has no
    legacy ``#NN`` prefix — and the AI Alpha boards carry none — so every
    command printed the row id and the first pass of task 659 missed it. The
    proof it was missed: ``vkctl close`` printed ``closed task 659 (task 659)``
    for the ticket the board shows as **#658**, which is the sentence that
    started this ticket, emitted by the tool meant to fix it.

    So this reads what each command actually writes, rather than the one field
    it is built from.
    """

    #: Every command that names the task it acted on, and how to invoke it.
    COMMANDS = {
        "show": ("show", "--number", str(TRAP_NUMBER)),
        "comment": ("comment", "--number", str(TRAP_NUMBER), "A note."),
        "move": ("move", "--number", str(TRAP_NUMBER), "Done"),
        "close": ("close", "--number", str(TRAP_NUMBER)),
    }

    def test_no_command_prints_the_row_id(self):
        for name, argv in self.COMMANDS.items():
            with self.subTest(command=name):
                code, out = self.run_vkctl(*argv)
                self.assertEqual(code, 0, out)
                self.assertNotIn(
                    str(TRAP_ROW_ID), out,
                    f"{name} printed the row id: {out!r}")

    def test_every_command_names_the_board_number(self):
        for name, argv in self.COMMANDS.items():
            with self.subTest(command=name):
                code, out = self.run_vkctl(*argv)
                self.assertEqual(code, 0, out)
                self.assertIn(f"#{TRAP_NUMBER}", out, f"{name}: {out!r}")

    def test_a_create_reports_the_number_the_board_will_show(self):
        """Read from the create reply, not derived — nothing can guess an index."""
        code, out = self.run_vkctl("create", "A new ticket")
        self.assertEqual(code, 0, out)
        self.assertIn("A new ticket", out)
        self.assertRegex(out, r"created #\d+")


class TestATitleWithNoLegacyPrefixIsTheLiveCase(CliTestCase):
    """The fixture every other test uses cannot catch this one.

    ``Ticket.reference`` is ``#NN`` when the title carries the legacy prefix
    and falls back to ``task <row id>`` when it does not. Every fixture in this
    suite carries one — "#34 Version the OpenClaw health-check skill" — so
    ``reference`` never reaches its fallback and a command printing it looks
    correct in the tests and prints a row id on the real board, which carries
    no prefixes at all since 2026-07-26.

    That is not hypothetical: it is how the first pass of task 659 shipped a
    ``show`` whose headline was the row id, with the whole suite green. A
    mutation restoring ``reference`` there survived until this class existed.
    """

    def setUp(self):
        super().setUp()
        for tasks in self.vikunja.layout.values():
            for stored in tasks:
                # Strip the prefix the live boards no longer carry.
                stored["title"] = stored["title"].split(" ", 1)[-1]

    def test_show_still_names_the_board_number(self):
        code, out = self.run_vkctl("show", "--number", str(TRAP_NUMBER))
        self.assertEqual(code, 0, out)
        headline = out.splitlines()[0]
        self.assertIn(f"#{TRAP_NUMBER}", headline, headline)
        self.assertNotIn(str(TRAP_ROW_ID), headline, headline)

    def test_no_command_falls_back_to_the_row_id(self):
        for name, argv in TestEveryLineItPrintsNamesTheBoardNumber.COMMANDS.items():
            with self.subTest(command=name):
                code, out = self.run_vkctl(*argv)
                self.assertEqual(code, 0, out)
                self.assertNotIn(f"task {TRAP_ROW_ID}", out, f"{name}: {out!r}")
                self.assertIn(f"#{TRAP_NUMBER}", out, f"{name}: {out!r}")


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
