"""A task is identified by its board and its number on that board (task 659).

Task 649 made ``project_id + task_number`` the identifier the tools accept,
and published Vikunja's immutable row id beside every answer as
``vikunja_task_id`` — named so it could not be mistaken for something
callable, on the reasoning that debug metadata is harmless.

It was not. A second number in the answer is a second number a reader can
quote, and one duly did: a branch and a commit message went out naming
"task 659" for the ticket the board shows as **#658**, because the answer
carried both numbers and only one of them is the ticket. Naming a field
carefully is not the same as not publishing it, and an id the tools refuse to
*accept* is an id they have no reason to *hand out*.

So the id is internal now. It is still load-bearing there — the resolver binds
it, the approval token binds it, the mutation ledger records it — and that is
the distinction these tests hold: a row is not an identity.

The remaining carrier is deliberate and named here rather than left implicit:
``url`` is ``/tasks/<id>``, because that is Vikunja's only task route and a
link whose job is to be opened is a locator, not an identifier. It is the one
place a row id still reaches a reader, and the test below pins it to exactly
that key so a third carrier cannot appear unnoticed.
"""

from __future__ import annotations

import unittest

from .fakes import PROJECT_ID
from .support import McpTestCase

#: The key task 649 published and task 659 removed.
RETIRED_KEY = "vikunja_task_id"


def _numbers(payload, out=None):
    """Every integer the payload publishes, with the key path that carries it."""
    out = [] if out is None else out
    if isinstance(payload, dict):
        for key, value in payload.items():
            _numbers(value, out) if isinstance(value, (dict, list)) else (
                out.append((key, value)) if isinstance(value, int)
                and not isinstance(value, bool) else None
            )
    elif isinstance(payload, list):
        for item in payload:
            _numbers(item, out)
    return out


class TestTheAnswerCarriesNoRowId(McpTestCase):
    """Read, list, search, create — none of them publishes the row id."""

    def _answers(self) -> dict[str, dict]:
        preview = self.service.update_task(8, title="Renamed")
        return {
            "get_task": self.service.get_task(8),
            "list_open_tasks": self.service.list_open_tasks(),
            "search_tasks": self.service.search_tasks("Vikunja", status="any"),
            "create_task": self.service.create_task(
                PROJECT_ID, "A ticket", "A body."),
            "update_task (preview)": preview,
            "update_task (applied)": self.service.update_task(
                8, title="Renamed", approval_token=preview["approval_token"]),
        }

    def test_no_answer_carries_the_retired_key(self):
        for name, answer in self._answers().items():
            with self.subTest(tool=name):
                self.assertNotIn(RETIRED_KEY, str(answer.keys()), name)
                self.assertNotIn(RETIRED_KEY, repr(answer), name)

    def test_no_answer_carries_a_row_id_under_any_other_key(self):
        """The stronger form: renaming the key would not satisfy this ticket.

        Asked PER TASK, against that task's own row id, because the fixture
        deliberately overlaps the two sets — task 649 built it so that some
        board numbers ARE some other task's id, which is the confusion the
        whole thing exists to catch. So "no published integer is any row id"
        cannot be asserted; "no published integer is THIS row's id" can, and
        the fixture guarantees a task's number never equals its own id.

        ``url`` is a string and so is not reached by this walk — the one
        exemption, named in the module docstring and pinned by the test below.
        """
        for name, answer in self._answers().items():
            for entry in answer.get("tasks", [answer]):
                number = entry.get("task_number")
                if number is None:
                    continue
                own_id = self.vikunja.id_of(number)
                self.assertNotEqual(number, own_id, "fixture cannot catch a swap")
                for key, value in _numbers(entry):
                    with self.subTest(tool=name, key=key):
                        self.assertNotEqual(
                            value, own_id, f"{name}.{key} publishes the row id")

    def test_the_url_is_the_one_place_a_row_id_still_reaches_a_reader(self):
        """Named, so a second carrier cannot arrive unnoticed.

        ``/tasks/<id>`` is Vikunja's only task route, so the alternative to
        this is no link at all. A locator is not an identifier — but it is the
        thing a reader copies digits out of, which is how this ticket started,
        so it is pinned rather than assumed.
        """
        answer = self.service.get_task(8)
        row_id = self.vikunja.id_of(answer["task_number"])
        self.assertEqual(answer["url"], f"http://127.0.0.1:3456/tasks/{row_id}")
        self.assertNotEqual(answer["task_number"], row_id)


class TestTheIdentityIsTheBoardAndTheNumber(McpTestCase):
    def test_every_answer_names_both_halves(self):
        """A project-local number means nothing without its project."""
        for name, answer in {
            "get_task": self.service.get_task(8),
            "list_open_tasks": self.service.list_open_tasks(),
        }.items():
            with self.subTest(tool=name):
                self.assertIn("project_id", answer)
                self.assertIn("project", answer)


# "One number on two boards is two tasks" is task 649's property and lives in
# tests/test_mcp_projects.py, which has the fixture for it. Removing the row id
# does not touch it, and restating it here would be a second copy to keep true.

if __name__ == "__main__":
    unittest.main()
