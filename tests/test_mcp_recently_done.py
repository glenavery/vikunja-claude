"""Enumerating finished work (task 664).

`list_open_tasks` never includes a done task, by contract. `get_task` needs a
number and `search_tasks` needs text — both require knowing what you are
looking for. So nothing answered "what was closed this week", and a ticket
closed as the last step of finishing it looked, to someone reading the board,
like a ticket that had never existed. That is not a hypothetical: it is how
this ticket was filed.
"""

from __future__ import annotations

import unittest

from vikunja_claude.mcp_service import MAX_RECENTLY_DONE, ToolError

from .fakes import PROJECT_ID, TRADER_PROJECT_ID, task
from .support import McpTestCase

_DONE = {"done": True}


def _layout_with_done(count: int) -> dict:
    """A board carrying `count` finished tasks, newest last by `updated`."""
    return {
        "Ready": [task(500, "Still open", "2026-07-01T00:00:00Z", index=1)],
        "Done": [
            task(600 + i, f"Finished {i}", "2026-07-01T00:00:00Z",
                 index=100 + i, **_DONE, updated=f"2026-08-{i + 1:02d}T00:00:00Z")
            for i in range(count)
        ],
    }


class TestItReturnsOnlyFinishedWork(McpTestCase):
    layout = _layout_with_done(3)

    def test_open_tasks_are_absent(self):
        answer = self.service.list_recently_done()
        titles = [t["title"] for t in answer["tasks"]]
        self.assertNotIn("Still open", titles)
        self.assertEqual(len(titles), 3)
        self.assertTrue(all(t["status"] == "done" for t in answer["tasks"]))

    def test_newest_first(self):
        answer = self.service.list_recently_done()
        updated = [t["updated"] for t in answer["tasks"]]
        self.assertEqual(updated, sorted(updated, reverse=True))

    def test_it_names_the_board_it_listed(self):
        answer = self.service.list_recently_done()
        self.assertEqual(answer["project_id"], PROJECT_ID)
        self.assertIn("project", answer)

    def test_the_complement_of_list_open_tasks(self):
        """Together they must cover the board and overlap nowhere — otherwise
        one of them is quietly dropping something."""
        done = {t["task_number"] for t in self.service.list_recently_done()["tasks"]}
        open_ = {t["task_number"] for t in self.service.list_open_tasks()["tasks"]}
        self.assertEqual(done & open_, set())
        self.assertTrue(done and open_)


class TestTheWindowStatesItsOwnEdge(McpTestCase):
    layout = _layout_with_done(8)

    def test_a_limit_is_applied_and_the_answer_says_it_was(self):
        answer = self.service.list_recently_done(limit=3)
        self.assertEqual(answer["count"], 3)
        self.assertEqual(answer["done_total"], 8)
        self.assertTrue(answer["truncated"])

    def test_an_untruncated_answer_says_that_too(self):
        """`truncated` is present either way: a caller that has to infer it
        from `count == limit` gets it wrong on the exact boundary."""
        answer = self.service.list_recently_done(limit=8)
        self.assertFalse(answer["truncated"])
        self.assertEqual(answer["count"], answer["done_total"])

    def test_the_limit_is_capped_rather_than_honoured_without_bound(self):
        answer = self.service.list_recently_done(limit=10_000)
        self.assertLessEqual(answer["count"], MAX_RECENTLY_DONE)

    def test_a_nonsense_limit_does_not_produce_an_empty_answer(self):
        """Zero and negatives floor to one rather than answering "nothing is
        done", which is a different and wrong claim."""
        for limit in (0, -5):
            with self.subTest(limit=limit):
                self.assertEqual(self.service.list_recently_done(limit=limit)["count"], 1)


class TestTheBoardBoundary(McpTestCase):
    layout = _layout_with_done(2)

    def test_a_project_outside_the_approved_set_is_refused(self):
        with self.assertRaises(ToolError):
            self.service.list_recently_done(project_id=4242)

    def test_the_other_approved_board_is_listed_on_request(self):
        answer = self.service.list_recently_done(project_id=TRADER_PROJECT_ID)
        self.assertEqual(answer["project_id"], TRADER_PROJECT_ID)


class TestItPublishesNoRowId(McpTestCase):
    layout = _layout_with_done(2)

    def test_no_published_integer_is_a_listed_task_own_row_id(self):
        """The same question task 663 asks of every other answer."""
        answer = self.service.list_recently_done()
        for entry in answer["tasks"]:
            own_id = self.vikunja.id_of(entry["task_number"])
            self.assertNotEqual(entry["task_number"], own_id, "fixture cannot catch a swap")
            for key, value in entry.items():
                if isinstance(value, int) and not isinstance(value, bool):
                    with self.subTest(key=key):
                        self.assertNotEqual(value, own_id)
            self.assertNotIn("url", entry)


if __name__ == "__main__":
    unittest.main()
