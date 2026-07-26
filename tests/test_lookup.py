"""Ticket lookup: #NN parsing, resolution, next-Ready selection."""

from __future__ import annotations

import unittest

from vikunja_claude.vikunja import (
    AmbiguousTicket,
    TicketNotFound,
    ticket_number,
)

from .fakes import task
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
        ticket = self.service.get(33)
        self.assertEqual(ticket.task_id, 9)
        self.assertEqual(ticket.bucket_title, "Ready")
        self.assertEqual(ticket.summary, "Back up Vikunja database")

    def test_description_html_is_flattened(self):
        ticket = self.service.get(33)
        self.assertIn("authoritative", ticket.description)
        self.assertIn("- cover `vikunja-db`", ticket.description)
        self.assertNotIn("<p>", ticket.description)

    def test_labels_are_carried_through(self):
        self.assertEqual(self.service.get(33).labels, ["Operations"])

    def test_unknown_ticket_raises_not_found(self):
        with self.assertRaises(TicketNotFound):
            self.service.get(999)

    def test_duplicate_numbers_are_ambiguous_not_arbitrary(self):
        self.vikunja.layout["Backlog"].append(
            task(77, "#33 A second ticket claiming 33", "2026-07-26T06:00:00Z")
        )
        with self.assertRaises(AmbiguousTicket) as caught:
            self.service.get(33)
        self.assertIn("#33 matches 2 tasks", str(caught.exception))

    def test_tasks_without_a_number_are_still_usable(self):
        """Identity is the task id, so a missing #NN prefix is not fatal."""
        self.vikunja.layout["Ready"].append(
            task(78, "No ticket prefix at all", "2026-07-26T06:00:00Z")
        )
        ticket = self.service.get_task(78)
        self.assertIsNone(ticket.number)
        self.assertEqual(ticket.summary, "No ticket prefix at all")
        self.assertEqual(ticket.reference, "task 78")
        self.assertEqual(ticket.commit_ref, "(vikunja task 78)")


class TaskIdLookup(ServiceTestCase):
    """The canonical lookup: Vikunja's immutable task id."""

    def test_finds_the_task_by_id(self):
        ticket = self.service.get_task(9)
        self.assertEqual(ticket.number, 33)
        self.assertEqual(ticket.summary, "Back up Vikunja database")
        self.assertEqual(ticket.reference, "#33")

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
            task(77, "#33 A second ticket claiming 33", "2026-07-26T06:00:00Z")
        )
        self.assertEqual(self.service.get_task(9).task_id, 9)
        self.assertEqual(self.service.get_task(77).task_id, 77)


class NextReadyTicket(ServiceTestCase):
    def test_picks_the_oldest_ready_ticket(self):
        ticket = self.service.next_ready()
        self.assertEqual(ticket.number, 33)

    def test_ignores_other_buckets(self):
        self.vikunja.layout["Backlog"].append(
            task(80, "#20 Ancient backlog item", "2020-01-01T00:00:00Z")
        )
        self.assertEqual(self.service.next_ready().number, 33)

    def test_ignores_done_tickets_left_in_ready(self):
        self.vikunja.layout["Ready"].insert(
            0, task(81, "#21 Old but done", "2020-01-01T00:00:00Z", done=True)
        )
        self.assertEqual(self.service.next_ready().number, 33)

    def test_empty_ready_bucket_raises_not_found(self):
        self.vikunja.layout["Ready"] = []
        with self.assertRaises(TicketNotFound):
            self.service.next_ready()


if __name__ == "__main__":
    unittest.main()
