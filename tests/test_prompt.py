"""Prompt generation: what the launched Claude is told, and what it is not."""

from __future__ import annotations

import re
import unittest

from .fakes import task
from .support import TOKEN, ServiceTestCase


class PromptGeneration(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = self.service.get_by_task_number(8)
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

    def test_states_the_incremental_validation_order(self):
        """The ORDER, not merely the words (task 823).

        Every step of this loop is advice any run would claim to follow already;
        what the #813 run actually did was apply several test edits and validate
        at the end. So the assertion is that the seven steps appear in the
        served prompt in the sequence they must happen in — a rewrite that
        preserved the vocabulary and lost the sequence would keep the defect and
        pass a presence check.
        """
        steps = [
            "smallest coherent change",
            "Apply that one change",
            "syntax- or type-check the files you just touched",
            "Run the narrowest existing tests",
            "before you make any further change",
            "Add regression tests the same way, one at a time",
            "Run the broader suite only once the narrow checks pass",
        ]
        positions = []
        for step in steps:
            index = self.prompt.find(step)
            self.assertNotEqual(index, -1, f"the prompt never says {step!r}")
            positions.append(index)
        self.assertEqual(positions, sorted(positions),
                         "the validation steps are out of order in the prompt")

    def test_a_failure_is_resolved_before_the_next_change(self):
        """The rule the loop rests on: the run stops advancing on a red state.

        Without it the order is a suggestion — a run can follow every step and
        still stack a second edit on a broken first one, which is the shape the
        #813 run got into.
        """
        self.assertIn("Fix any failure", self.prompt)
        self.assertIn("a syntax or LSP error, a failing test", self.prompt)
        self.assertIn("before you make any further change", self.prompt)

    def test_validation_after_every_edit_is_narrow_never_the_full_suite(self):
        """Both halves, because either alone is the wrong instruction.

        "Validate after every edit" without the narrowing reads as a full suite
        per edit, which is slow enough that a run learns to skip the step; the
        narrowing without the obligation is permission to batch.
        """
        self.assertIn("You do not need the full suite after every edit",
                      self.prompt)
        self.assertIn("you do need step c and step\n   d after every edit",
                      self.prompt)

    def test_the_loop_is_in_the_shared_prompt_and_not_per_executor(self):
        """One policy for every harness (task 823's own requirement).

        Asked of the builder's signature rather than of the text: `build_prompt`
        cannot vary the loop by executor because it is never told which executor
        will run — the runner picks that afterwards. A branch on the harness
        would have to appear as a parameter here first.
        """
        import inspect

        from vikunja_claude.prompt import build_prompt

        parameters = set(inspect.signature(build_prompt).parameters)
        for forbidden in ("executor", "harness", "model", "is_local"):
            self.assertNotIn(forbidden, parameters)

    def test_says_when_to_ask_the_code_graph(self):
        """Task 825. #821 connected Graphify to the local harness and the
        restarted #813 run went on grepping, because nothing in the prompt said
        the graph existed or when it answers better. The rule has to name the
        question shape it is for, not merely the tool."""
        # The template wraps these sentences, so match on unwrapped fragments —
        # the convention the tests above this one already follow.
        self.assertIn("NAVIGATING THE CODE", self.prompt)
        self.assertIn("ask it first for RELATIONSHIP", self.prompt)
        self.assertIn("questions: who calls this", self.prompt)
        self.assertIn("Then read the specific files it", self.prompt)

    def test_distinguishes_relationship_navigation_from_literal_search(self):
        """Both halves. A rule that only promoted the graph would push a run to
        ask it for an exact string, which is what search is good at and the
        graph is not; the run would then conclude the graph is useless."""
        self.assertIn("LITERAL question", self.prompt)
        self.assertIn("this exact string, flag or error message", self.prompt)
        self.assertIn("Text search is still the right tool", self.prompt)

    def test_requires_reading_the_resolved_source_before_changing_it(self):
        """The graph describes the code; only the code is the code. Editing on
        what the graph said would make a stale or partial index into a wrong
        edit, which is worse than the grepping this replaces."""
        self.assertIn("Never change code on what the graph said alone",
                      self.prompt)
        self.assertIn("open the source", self.prompt)

    def test_stays_conditional_when_no_graph_is_available(self):
        """A repository without a graph must not read this as a blocked run.

        The clause is conditional at both ends — "if this repository has a code
        graph" opening it, and an explicit fallback closing it — because a run
        that treats an absent tool as a prerequisite stops instead of grepping.
        """
        self.assertIn("If this repository has a code graph", self.prompt)
        self.assertIn("if no graph is available here, navigate by search",
                      self.prompt)
        self.assertIn("not a step you are required to have taken", self.prompt)

    def test_the_navigation_rule_did_not_disturb_the_rules_around_it(self):
        """Task 825 inserted a rule and renumbered; nothing else may have moved.

        The renumbering is the risk an insertion carries — the old rule 3 became
        4 and so on — so this pins that each surviving rule still leads its own
        numbered line, and that the one cross-reference between them was
        rewritten to name its rule rather than a number that keeps changing.
        """
        for numbered in ("1. SCOPE.", "2. HOW YOU WORK.",
                         "3. NAVIGATING THE CODE.", "4. TESTS.",
                         "5. WHERE YOU ARE.", "6. COMMIT.", "7. DO NOT PUSH.",
                         "8. REPORT BACK"):
            self.assertIn(numbered, self.prompt)
        self.assertIn("the human step the WHERE YOU ARE rule names", self.prompt)
        self.assertNotIn("rule 3 names", self.prompt)
        self.assertNotIn("rule 4 names", self.prompt)

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

    def test_success_path_moves_to_waiting_never_done(self):
        """A finished run has a commit on its own worktree branch and nothing
        else: the runner does not merge, and merging is the human step. Done
        would claim the work reached main, so the success path stops at
        Waiting and no path in the prompt moves a ticket to Done."""
        self.assertIn("vkctl.py comment --number 8", self.prompt)
        self.assertIn("vkctl.py move --number 8 Waiting", self.prompt)
        self.assertIsNone(
            re.search(r"move\s+--number\s+8\s+Done", self.prompt),
            "the prompt still tells a successful run to move the ticket to Done",
        )

    def test_success_comment_names_the_commit_and_the_merge_still_owed(self):
        """The comment is what a human reads to know what is left, so it must
        carry the sha and say the merge into main has not happened."""
        self.assertIn("the commit sha", self.prompt)
        self.assertIn("merged into main before this ticket is Done", self.prompt)

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
        self.assertIn(str(self.workdir), self.prompt)

    def test_never_contains_the_vikunja_token(self):
        self.assertNotIn(TOKEN, self.prompt)
        self.assertNotIn("VIKUNJA_API_TOKEN", self.prompt)

    def test_empty_description_is_stated_not_silently_blank(self):
        self.vikunja.layout["Ready"].append(
            task(90, "#90 Bare ticket", "2026-07-26T07:00:00Z", "", index=89)
        )
        prompt = self.service.prompt_for(self.service.get_by_task_number(89))
        self.assertIn("(no description on the ticket)", prompt)


if __name__ == "__main__":
    unittest.main()
