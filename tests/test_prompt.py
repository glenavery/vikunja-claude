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

    def test_identifies_the_ticket_by_its_board_number(self):
        """#8 is this fixture's board index; 33 is a legacy title prefix and 9
        is its row id. The board number is the ticket (task 659)."""
        self.assertIn("TICKET #8: Back up Vikunja database", self.prompt)

    def test_includes_the_complete_description(self):
        for fragment in self.ticket.description.splitlines():
            if fragment.strip():
                self.assertIn(fragment.strip(), self.prompt)

    def test_description_is_delimited_so_it_cannot_bleed_into_instructions(self):
        self.assertIn("--- BEGIN TICKET DESCRIPTION ---", self.prompt)
        self.assertIn("--- END TICKET DESCRIPTION ---", self.prompt)

    def test_limits_work_to_this_ticket(self):
        self.assertIn("Do only what ticket #8 asks", self.prompt)
        self.assertIn("do not start other tickets", self.prompt)

    def test_requires_tests(self):
        self.assertIn("not finished without tests", self.prompt)
        self.assertIn("would fail without your change", self.prompt)
        # The template wraps this sentence, so match on unwrapped words.
        self.assertIn("Never weaken,", self.prompt)
        self.assertIn("to make a failure disappear", self.prompt)

    def test_requires_a_commit_referencing_the_ticket(self):
        self.assertIn("(#8)", self.prompt)
        self.assertIn("COMMIT.", self.prompt)

    def test_forbids_pushing(self):
        self.assertIn("DO NOT PUSH", self.prompt)
        self.assertIn("no pull request", self.prompt)

    def test_blocked_path_moves_to_waiting_with_a_comment(self):
        self.assertIn("BLOCKED:", self.prompt)
        self.assertIn("vkctl.py move --number 8 Waiting", self.prompt)

    def test_success_path_moves_to_done_with_a_comment(self):
        self.assertIn("vkctl.py comment --number 8", self.prompt)
        self.assertIn("vkctl.py move --number 8 Done", self.prompt)

    def test_the_prompt_hands_the_run_no_row_id_at_all(self):
        """THE DEFECT THIS GUARDS, and it is the one task 659 was filed for.

        The prompt is copied verbatim into every run, so whatever number it
        names is the number that reaches branches, commit messages and the
        closing vkctl call. It named the row id in five places -- the TICKET
        heading, a "Vikunja task id" line, the commit example and both vkctl
        commands -- which is how this repository's own history carries four
        commits reading "(vikunja task 660)" for board #659, and how a later
        session closed the wrong ticket with `--task`.

        The URL was left as a deliberate exemption and is gone now (task 663).
        It printed the row id two lines above the rule telling the run never to
        read a number out of a /tasks/<id> URL — the warning and the hazard in
        one document, and the hazard is the half that gets copied.
        """
        # Asked as "no form that NAMES a task by its row id", not as "these
        # digits are absent". The fixture's row id is 9, and a bare "9" occurs
        # in any path that happens to contain the digit — this test passed in
        # the main checkout and failed inside a worktree called task-669,
        # which makes it a test of the directory name rather than of the
        # prompt. A one-character needle is not an assertion.
        row = self.ticket.task_id
        for form in (f"/tasks/{row}", f"--task {row}", f"task {row}",
                     f"task id {row}", f"TICKET task {row}"):
            self.assertNotIn(form, self.prompt,
                             f"the prompt names the row id as {form!r}")
        self.assertNotIn("Vikunja URL:", self.prompt)
        self.assertNotIn("--task", self.prompt)
        self.assertNotIn("vikunja task", self.prompt)
        # The RULE against reading a number out of that route stays; only the
        # printed instance of one is gone.
        self.assertIn("/tasks/<id>", self.prompt)

    def test_a_task_with_no_board_number_says_so_rather_than_inventing_one(self):
        """The one case where the row id is the only name there is. It must
        still be labelled, never emitted as a bare number that reads as a board
        number -- the two spaces overlap, so a bare digit is the ambiguity."""
        self.vikunja.layout["Ready"].append(
            task(91, "Indexless", "2026-07-26T07:00:00Z", "body", index=None)
        )
        prompt = self.service.prompt_for(self.service.get_task(91))
        self.assertIn("TICKET task 91 (no project-local number)", prompt)
        self.assertIn("--task 91", prompt)
        self.assertNotIn("--number None", prompt)

    def test_names_the_repository(self):
        self.assertIn("/home/glen/stacks/investment", self.prompt)

    def test_never_contains_the_vikunja_token(self):
        self.assertNotIn(TOKEN, self.prompt)
        self.assertNotIn("VIKUNJA_API_TOKEN", self.prompt)

    def test_empty_description_is_stated_not_silently_blank(self):
        self.vikunja.layout["Ready"].append(
            task(90, "#90 Bare ticket", "2026-07-26T07:00:00Z", "", index=89)
        )
        prompt = self.service.prompt_for(self.service.get(90))
        self.assertIn("(no description on the ticket)", prompt)


if __name__ == "__main__":
    unittest.main()
