"""Prompt generation: what the launched Claude is told, and what it is not."""

from __future__ import annotations

import unittest

from .fakes import task
from .support import TOKEN, ServiceTestCase


class PromptGeneration(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = self.service.get(33)
        self.prompt = self.service.prompt_for(self.ticket)

    def test_identifies_the_ticket(self):
        self.assertIn("TICKET #33: Back up Vikunja database", self.prompt)
        self.assertIn("Vikunja task id: 9", self.prompt)
        self.assertIn("http://127.0.0.1:3456/tasks/9", self.prompt)

    def test_includes_the_complete_description(self):
        for fragment in self.ticket.description.splitlines():
            if fragment.strip():
                self.assertIn(fragment.strip(), self.prompt)

    def test_description_is_delimited_so_it_cannot_bleed_into_instructions(self):
        self.assertIn("--- BEGIN TICKET DESCRIPTION ---", self.prompt)
        self.assertIn("--- END TICKET DESCRIPTION ---", self.prompt)

    def test_limits_work_to_this_ticket(self):
        self.assertIn("Do only what ticket #33 asks", self.prompt)
        self.assertIn("do not start other tickets", self.prompt)

    def test_requires_tests(self):
        self.assertIn("not finished without tests", self.prompt)
        self.assertIn("would fail without your change", self.prompt)
        # The template wraps this sentence, so match on unwrapped words.
        self.assertIn("Never weaken,", self.prompt)
        self.assertIn("to make a failure disappear", self.prompt)

    def test_requires_a_commit_referencing_the_ticket(self):
        self.assertIn("(#33)", self.prompt)
        self.assertIn("COMMIT.", self.prompt)

    def test_forbids_pushing(self):
        self.assertIn("DO NOT PUSH", self.prompt)
        self.assertIn("no pull request", self.prompt)

    def test_blocked_path_moves_to_waiting_with_a_comment(self):
        self.assertIn("BLOCKED:", self.prompt)
        self.assertIn("vkctl.py move 33 Waiting", self.prompt)

    def test_success_path_moves_to_done_with_a_comment(self):
        self.assertIn("vkctl.py comment 33", self.prompt)
        self.assertIn("vkctl.py move 33 Done", self.prompt)

    def test_names_the_repository(self):
        self.assertIn("/home/glen/stacks/investment", self.prompt)

    def test_never_contains_the_vikunja_token(self):
        self.assertNotIn(TOKEN, self.prompt)
        self.assertNotIn("VIKUNJA_API_TOKEN", self.prompt)

    def test_empty_description_is_stated_not_silently_blank(self):
        self.vikunja.layout["Ready"].append(
            task(90, "#90 Bare ticket", "2026-07-26T07:00:00Z", "")
        )
        prompt = self.service.prompt_for(self.service.get(90))
        self.assertIn("(no description on the ticket)", prompt)


if __name__ == "__main__":
    unittest.main()
