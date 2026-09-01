"""Ticket lookup: #NN parsing, resolution, next-Ready selection."""

from __future__ import annotations

import re
import unittest

from vikunja_claude.service import TicketService
from vikunja_claude.vikunja import (
    AmbiguousTicket,
    TicketNotFound,
    VikunjaClient,
    ticket_number,
)

from .fakes import FilterIgnoringVikunja, FilterMatchingNothingVikunja, task
from .support import ServiceTestCase


class TicketNumberParsing(unittest.TestCase):
    def test_reads_the_hash_prefix(self):
        self.assertEqual(ticket_number("#33 Back up Vikunja database"), 33)

    def test_allows_leading_whitespace(self):
        self.assertEqual(ticket_number("  #7 Something"), 7)

    def test_ignores_hashes_that_are_not_a_prefix(self):
        self.assertIsNone(ticket_number("Back up database #33"))

    def test_untitled_or_unnumbered_is_none(self):
        self.assertIsNone(ticket_number(""))
        self.assertIsNone(ticket_number("No number here"))
        self.assertIsNone(ticket_number("#abc not a number"))

    def test_multi_digit(self):
        self.assertEqual(ticket_number("#1234 Big backlog"), 1234)


class TicketLookup(ServiceTestCase):
    def test_finds_ticket_by_number(self):
        ticket = self.service.get_by_task_number(8)
        self.assertEqual(ticket.task_id, 9)
        self.assertEqual(ticket.bucket_title, "Ready")
        self.assertEqual(ticket.summary, "Back up Vikunja database")

    def test_description_html_is_flattened(self):
        ticket = self.service.get_by_task_number(8)
        self.assertIn("authoritative", ticket.description)
        self.assertIn("- cover `vikunja-db`", ticket.description)
        self.assertNotIn("<p>", ticket.description)

    def test_labels_are_carried_through(self):
        self.assertEqual(self.service.get_by_task_number(8).labels, ["Operations"])

    def test_unknown_ticket_raises_not_found(self):
        with self.assertRaises(TicketNotFound):
            self.service.get_by_task_number(999)

    def test_duplicate_numbers_are_ambiguous_not_arbitrary(self):
        """Two tasks answering to one board number is refused, not resolved."""
        self.vikunja.layout["Backlog"].append(
            task(77, "A second task claiming board number 8",
                 "2026-07-26T06:00:00Z", index=8)
        )
        with self.assertRaises(AmbiguousTicket) as caught:
            self.service.get_by_task_number(8)
        self.assertIn("#8 matches 2 tasks", str(caught.exception))

    def test_a_legacy_title_prefix_is_not_a_board_number(self):
        """The two schemes are not each other, and this lookup takes one.

        Fixture task 9 is titled ``#33 ...`` and sits at board number 8. Asking
        for 33 must miss rather than fall back to the prefix: on a real board
        the numbers disagree by a few, so a fallback would usually return some
        other real task (task 748).
        """
        with self.assertRaises(TicketNotFound):
            self.service.get_by_task_number(33)

    def test_tasks_without_a_number_are_still_usable(self):
        """Identity is the task id, so a missing #NN prefix is not fatal."""
        self.vikunja.layout["Ready"].append(
            task(78, "No ticket prefix at all", "2026-07-26T06:00:00Z", index=77)
        )
        ticket = self.service.get_task(78)
        self.assertIsNone(ticket.number)
        self.assertEqual(ticket.summary, "No ticket prefix at all")
        # The board still numbers it, so the board number is still its name --
        # the legacy prefix was never the identity (task 659).
        self.assertEqual(ticket.board_reference, "#77")
        self.assertEqual(ticket.commit_ref, "(#77)")


class TaskIdLookup(ServiceTestCase):
    """The canonical lookup: Vikunja's immutable task id."""

    def test_finds_the_task_by_id(self):
        ticket = self.service.get_task(9)
        self.assertEqual(ticket.number, 33)
        self.assertEqual(ticket.summary, "Back up Vikunja database")
        # Legacy prefix 33, row id 9, board number 8 -- deliberately all
        # different, and only one of them names the ticket.
        self.assertEqual(ticket.board_reference, "#8")

    def test_unknown_task_id_is_not_found(self):
        with self.assertRaises(TicketNotFound):
            self.service.get_task(4242)

    def test_not_found_message_warns_about_board_view_urls(self):
        with self.assertRaises(TicketNotFound) as caught:
            self.service.get_task(4242)
        self.assertIn("/projects/N/M is a board view", str(caught.exception))

    def test_task_id_is_stable_when_the_title_prefix_changes(self):
        """Renumbering the title must not change which task is resolved."""
        before = self.service.get_task(9)
        for item in self.vikunja.layout["Ready"]:
            if item["id"] == 9:
                item["title"] = "#99 Back up Vikunja database"
        after = self.service.get_task(9)
        self.assertEqual(before.task_id, after.task_id)
        self.assertEqual(before.number, 33)
        self.assertEqual(after.number, 99)

    def test_duplicate_hash_prefixes_do_not_affect_task_id_lookup(self):
        self.vikunja.layout["Backlog"].append(
            task(77, "#33 A second ticket claiming 33", "2026-07-26T06:00:00Z", index=76)
        )
        self.assertEqual(self.service.get_task(9).task_id, 9)
        self.assertEqual(self.service.get_task(77).task_id, 77)


#: Bigger than one Vikunja page (50), so the tasks below live on page 3 and are
#: unreachable to anything that reads a single page of the bucket.
DEEP_DONE = [
    task(
        2000 + i,
        f"#{2000 + i} Closed long ago",
        "2026-07-20T00:00:00Z",
        done=True,
        index=1999 + i,
    )
    for i in range(120)
]


class ADeepBucketIsStillReachable(ServiceTestCase):
    """Task 185: `get_task` denied task 35, which exists.

    Vikunja pages tasks *inside* a bucket at 50 and ignores `per_page`, so the
    Done column on the live board (170 tasks) hid everything past the first
    page. The failure was the bad kind: a task that exists reported as absent,
    with a message blaming the caller for confusing a task id with a view id.
    """

    layout = {
        "Backlog": [task(5, "#29 Admin", "2026-07-26T05:01:00Z", index=4)],
        "Ready": [
            task(9, "#33 Back up Vikunja database", "2026-07-26T05:05:50Z", index=8)
        ],
        "In Progress": [],
        "Waiting": [],
        "Done": DEEP_DONE,
    }

    DEEP = 2119  # the last task in Done: page 3 of that bucket

    def test_the_bucket_really_is_deeper_than_one_page(self):
        """Guards every test below: with a 50-task Done none of them mean anything."""
        page = self.client._view_page(2, 12, 1, None)
        done = next(b for b in page if b["title"] == "Done")
        self.assertEqual(len(done["tasks"]), 50)
        self.assertEqual(done["count"], len(DEEP_DONE))
        self.assertNotIn(self.DEEP, {t["id"] for t in done["tasks"]})

    def test_a_task_on_the_third_page_resolves(self):
        ticket = self.service.get_task(self.DEEP)
        self.assertEqual(ticket.task_id, self.DEEP)
        self.assertEqual(ticket.bucket_title, "Done")
        self.assertTrue(ticket.done)

    def test_every_task_in_the_deep_bucket_resolves(self):
        for item in DEEP_DONE:
            self.assertEqual(self.service.get_task(item["id"]).task_id, item["id"])

    def test_the_hash_prefix_lookup_reaches_it_too(self):
        ticket = self.service.get_by_task_number(2118)
        self.assertEqual(ticket.task_id, self.DEEP)

    def test_a_genuinely_absent_task_is_still_not_found(self):
        """"Not found" has to keep meaning not present."""
        with self.assertRaises(TicketNotFound):
            self.service.get_task(4242)

    def test_a_shallow_task_still_resolves(self):
        self.assertEqual(self.service.get_task(9).number, 33)


class ALookupWhenTheFilterDoesNothing(ServiceTestCase):
    """The id filter is an optimisation; correctness may not depend on it.

    This is the harmless mode, and it is worth pinning as harmless: a filter
    the server ignores turns the single-id request back into a walk of the
    whole board, which is slower and still complete. Compare
    :class:`ALookupWhenTheFilterMatchesNothing`, which is the mode that bites.
    """

    layout = ADeepBucketIsStillReachable.layout
    DEEP = ADeepBucketIsStillReachable.DEEP

    def setUp(self) -> None:
        super().setUp()
        self.vikunja = FilterIgnoringVikunja(layout=self.layout)
        self.client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.service = TicketService(self.config, self.client, self.launcher)

    def test_the_filter_really_is_being_ignored(self):
        """Guards the test below: otherwise the fallback is never exercised."""
        served = self.client._view_page(2, 12, 1, "id = %d" % self.DEEP)
        self.assertGreater(sum(len(b.get("tasks") or []) for b in served), 1)

    def test_the_deep_task_still_resolves(self):
        self.assertEqual(self.service.get_task(self.DEEP).task_id, self.DEEP)

    def test_an_absent_task_is_still_not_found(self):
        with self.assertRaises(TicketNotFound):
            self.service.get_task(4242)


class ALookupWhenTheFilterMatchesNothing(ServiceTestCase):
    """A filtered miss is not an absence, and this is why.

    An ignored filter is the harmless mode: the server answers with the whole
    board and the walk over it stays complete. A filter that is *applied* and
    matches nothing is the harmful one — the answer is well-formed, `count`
    agrees with what was served, and nothing in it says the task is missing
    rather than unmatched.
    """

    layout = ADeepBucketIsStillReachable.layout
    DEEP = ADeepBucketIsStillReachable.DEEP

    def setUp(self) -> None:
        super().setUp()
        self.vikunja = FilterMatchingNothingVikunja(layout=self.layout)
        self.client = VikunjaClient(
            self.config.api_url, self.config.token, transport=self.vikunja
        )
        self.service = TicketService(self.config, self.client, self.launcher)

    def test_the_filter_really_does_match_nothing(self):
        """Guards the tests below: otherwise the fallback is never reached."""
        served = self.client._view_page(2, 12, 1, f"id = {self.DEEP}")
        self.assertEqual(sum(len(b.get("tasks") or []) for b in served), 0)
        self.assertEqual({b.get("count") for b in served}, {0})

    def test_a_shallow_task_still_resolves(self):
        self.assertEqual(self.service.get_task(9).number, 33)

    def test_the_deep_task_still_resolves(self):
        self.assertEqual(self.service.get_task(self.DEEP).task_id, self.DEEP)

    def test_an_absent_task_is_still_not_found(self):
        with self.assertRaises(TicketNotFound):
            self.service.get_task(4242)


class TheLookupStaysInsideItsProject(ServiceTestCase):
    """A task id that belongs to another project is refused, not served."""

    #: On somebody else's board. It is a real task with a real id.
    FOREIGN = 4242

    def setUp(self) -> None:
        super().setUp()
        self.vikunja.foreign = {
            self.FOREIGN: task(
                self.FOREIGN,
                "#1 Someone else's ticket",
                "2026-07-01T00:00:00Z",
                index=1,
            )
        }

    def test_the_foreign_task_really_is_fetchable_by_bare_id(self):
        """Guards the test below: otherwise 'refused' is just 'does not exist'."""
        fetched = self.client.call("GET", f"/tasks/{self.FOREIGN}")
        self.assertEqual(fetched["id"], self.FOREIGN)

    def test_it_is_still_not_found_on_this_board(self):
        with self.assertRaises(TicketNotFound):
            self.service.get_task(self.FOREIGN)

    def test_every_request_it_makes_names_this_project(self):
        with self.assertRaises(TicketNotFound):
            self.service.get_task(self.FOREIGN)
        viewed = [p for _, p, _ in self.vikunja.calls if p.startswith("/projects/")]
        self.assertTrue(viewed)
        for path in viewed:
            self.assertTrue(
                path.startswith("/projects/2"),
                f"the lookup read {path!r}, outside the allowed project",
            )

    def test_it_never_fetches_a_task_by_bare_id(self):
        """GET /tasks/{id} serves any task in any project, so the lookup does
        not use it — that is what keeps the project boundary structural."""
        with self.assertRaises(TicketNotFound):
            self.service.get_task(self.FOREIGN)
        for method, path, _ in self.vikunja.calls:
            self.assertIsNone(
                re.match(r"^/tasks/\d+$", path),
                f"the lookup issued {method} {path}, which is not project-scoped",
            )


class NextReadyTicket(ServiceTestCase):
    def test_picks_the_oldest_ready_ticket(self):
        ticket = self.service.next_ready()
        self.assertEqual(ticket.number, 33)

    def test_ignores_other_buckets(self):
        self.vikunja.layout["Backlog"].append(
            task(80, "#20 Ancient backlog item", "2020-01-01T00:00:00Z", index=79)
        )
        self.assertEqual(self.service.next_ready().number, 33)

    def test_ignores_done_tickets_left_in_ready(self):
        self.vikunja.layout["Ready"].insert(
            0, task(81, "#21 Old but done", "2020-01-01T00:00:00Z", done=True, index=80)
        )
        self.assertEqual(self.service.next_ready().number, 33)

    def test_empty_ready_bucket_raises_not_found(self):
        self.vikunja.layout["Ready"] = []
        with self.assertRaises(TicketNotFound):
            self.service.next_ready()


if __name__ == "__main__":
    unittest.main()
