"""Task 818 — the prompt carries the ticket's comments, not just its description.

A ticket is relaunched when the first run did not finish it, and the reason it
did not finish is almost always written in a comment. Until this, `build_prompt`
rendered the description alone, so a relaunch was handed the ticket as FILED
rather than the ticket as it STANDS.

Task #813 is the failure in full. Qwen's first run committed, commented, and
moved the ticket to Waiting. A review comment rejected that commit. #813 was
relaunched — and the second run received the same text the first one had, saw
no review, re-checked the rejected commit, agreed with itself, and returned the
ticket to Waiting. Nothing malfunctioned; the run answered the question it was
asked, and the question was out of date.

Three properties are what actually close it, and each has a test here:

  * the comments are in the prompt, in order, with who said what and when — the
    sequence is the whole signal, since "the review came after the completion
    claim" is what makes it a rejection rather than a note;
  * they are read at LAUNCH time, so a comment added between two runs of one
    ticket is in the second run's prompt;
  * a read that fails REFUSES the launch. Launching without them is the exact
    defect above, and it is invisible from the outside — the run looks fine, it
    just answers last week's question.
"""

from __future__ import annotations

import unittest

from vikunja_claude.executors import LOCAL_EXECUTOR
from vikunja_claude.prompt import build_prompt
from vikunja_claude.vikunja import VikunjaError

from .support import ServiceTestCase, make_config
# The one place that knows what a workdir a LOCAL run can launch from looks
# like. Imported rather than rebuilt so "both executors" here means the real
# local executor, not a second approximation of it.
from .test_executors import BASE_URL, write_repo

BEGIN = "--- BEGIN TICKET COMMENTS"
END = "--- END TICKET COMMENTS ---"

#: Task 9 is the Ready ticket the default layout provides; the board shows it
#: as #8. These two comments are #813's shape: a run reporting success, and a
#: human rejecting exactly what it reported.
TASK_813 = {
    9: [
        {
            "id": 41,
            "comment": "<p>Implemented the change. Tests pass. Commit "
            "<code>abc1234</code> on branch task-813-x, still to be merged.</p>",
            "created": "2026-09-01T09:00:00Z",
            "author": {"username": "qwen"},
        },
        {
            "id": 42,
            "comment": "<p>REVIEW: rejected. The commit does not do what the "
            "ticket asks — it only renames the helper.</p>",
            "created": "2026-09-02T08:30:00Z",
            "author": {"username": "glen"},
        },
    ]
}


class TheRelaunchSees813sReview(ServiceTestCase):
    """The regression. Both comments reach the run, in the order they happened."""

    comments = TASK_813

    def setUp(self) -> None:
        super().setUp()
        self.prompt = self.service.prompt_for(self.service.get_by_task_number(8))

    def test_the_completion_claim_and_the_review_are_both_in_the_prompt(self):
        self.assertIn("Implemented the change", self.prompt)
        self.assertIn("REVIEW: rejected", self.prompt)

    def test_the_review_comes_after_the_claim_it_rejects(self):
        """Order is the signal. The same two sentences the other way round say
        a rejection was answered and the work then done, which is the opposite
        of what happened."""
        self.assertLess(
            self.prompt.index("Implemented the change"),
            self.prompt.index("REVIEW: rejected"),
            "the review is rendered before the completion claim it rejects",
        )

    def test_each_comment_carries_its_author_and_its_time(self):
        """Enough to place a comment in the sequence without inferring it: who
        said it, and whether it was before or after the run that claimed to be
        finished."""
        self.assertIn("qwen at 2026-09-01T09:00:00Z", self.prompt)
        self.assertIn("glen at 2026-09-02T08:30:00Z", self.prompt)

    def test_they_are_numbered_within_a_stated_total(self):
        self.assertIn("(2, oldest first)", self.prompt)
        self.assertIn("[comment 1 of 2]", self.prompt)
        self.assertIn("[comment 2 of 2]", self.prompt)

    def test_the_comments_are_delimited_and_kept_out_of_the_description(self):
        """Other people's prose, fenced for the same reason the description is:
        it must not read as instructions, and it must not read as part of the
        brief as filed."""
        self.assertIn(BEGIN, self.prompt)
        self.assertIn(END, self.prompt)
        self.assertLess(
            self.prompt.index("--- END TICKET DESCRIPTION ---"),
            self.prompt.index(BEGIN),
        )
        self.assertLess(self.prompt.index(END), self.prompt.index("Rules for this run:"))

    def test_the_run_is_told_a_later_comment_overrides_the_description(self):
        """Present without this, the two blocks merely disagree, and the run is
        left to pick. #813 picked the description."""
        # The template wraps this sentence, so match on unwrapped words.
        self.assertIn("a later comment overrides an earlier one and overrides", self.prompt)
        self.assertIn("REJECTS the brief", self.prompt)

    def test_comment_html_is_flattened_like_every_other_read_surface(self):
        self.assertIn("`abc1234`", self.prompt)
        self.assertNotIn("<p>", self.prompt)


class AnUncommentedTicketLaunchesAsBefore(ServiceTestCase):
    """No comments, no section — the prompt a ticket has always had.

    Absence is unambiguous here without being spelled out, because a run only
    ever receives this prompt when the comments were read: a read that fails
    refuses the launch, which is `AReadThatFailsRefusesTheLaunch` below.
    """

    def test_no_comment_section_is_rendered(self):
        prompt = self.service.prompt_for(self.service.get_by_task_number(8))
        self.assertNotIn(BEGIN, prompt)
        self.assertNotIn(END, prompt)

    def test_the_description_and_the_rules_are_untouched(self):
        prompt = self.service.prompt_for(self.service.get_by_task_number(8))
        self.assertIn("--- END TICKET DESCRIPTION ---\n\nRules for this run:", prompt)

    def test_the_ticket_still_launches(self):
        self.assertTrue(self.service.work(self.service.get_by_task_number(8))["launched"])
        self.assertEqual(len(self.spawn.calls), 1)


class BuildPromptWillNotGuessAtComments(unittest.TestCase):
    """`comments` has no default, and that is the guarantee, not tidiness.

    A default of `[]` reintroduces #813 for any future caller that forgets one
    argument — in the one shape where the result looks like a ticket nobody had
    commented on.
    """

    def test_omitting_them_is_a_typeerror_not_an_empty_section(self):
        with self.assertRaises(TypeError):
            build_prompt(object(), "workdir", "AI Alpha Engine")


class TheyAreReadAtLaunchTime(ServiceTestCase):
    """A comment added between two runs of one ticket reaches the second one.

    Which is the whole point: the first run's prompt cannot contain the review
    of the first run. Nothing here may cache a ticket's comments from an
    earlier launch.
    """

    def test_a_comment_added_after_the_first_run_is_in_the_second_prompt(self):
        ticket = self.service.get_by_task_number(8)
        self.service.work(ticket)
        self.assertNotIn(BEGIN, self.spawn.calls[0]["argv"][-1])

        self.client.add_comment(
            ticket.task_id, "REVIEW: rejected, the helper is only renamed."
        )
        # The lock from the first launch is stale: its pid was never registered
        # as alive, which is how this suite models a run that has ended.
        self.service.work(self.service.get_by_task_number(8))

        relaunched = self.spawn.calls[1]["argv"][-1]
        self.assertIn(BEGIN, relaunched)
        self.assertIn("REVIEW: rejected, the helper is only renamed.", relaunched)


class AReadThatFailsRefusesTheLaunch(ServiceTestCase):
    """Incomplete context is not a degraded launch; it is #813 again.

    Read before anything is moved, for the same reason the executor is resolved
    before anything is moved: the refusal has to leave the board as it found
    it, rather than parking a ticket In Progress with nothing running.
    """

    comments = TASK_813

    def setUp(self) -> None:
        super().setUp()

        def refuse(task_id: int):
            raise VikunjaError("GET /tasks/9/comments failed: 503")

        self.client.comment_views = refuse

    def test_work_refuses_rather_than_launching_without_them(self):
        with self.assertRaises(VikunjaError):
            self.service.work(self.service.get_by_task_number(8))
        self.assertEqual(self.spawn.calls, [], "a run was launched with no comments")

    def test_the_ticket_is_left_in_the_column_it_was_in(self):
        with self.assertRaises(VikunjaError):
            self.service.work(self.service.get_by_task_number(8))
        self.assertEqual(
            self.service.get_by_task_number(8).bucket_title,
            "Ready",
            "the ticket was moved to In Progress for a run that never started",
        )


class ThePreviewShowsWhatTheRunWillGet(ServiceTestCase):
    """One assembled context, shown and sent.

    The preview publishes the comments beside the prompt. If it read them twice
    it could show a reader one brief and hand the run another — and the preview
    is the only place a human checks what a run is about to be told.
    """

    comments = TASK_813

    def test_the_previewed_prompt_carries_the_previewed_comments(self):
        data = self.service.preview(self.service.get_by_task_number(8))
        for comment in data["comments"]:
            self.assertIn(comment["text"], data["prompt"])

    def test_the_previewed_comment_block_is_the_launched_one(self):
        ticket = self.service.get_by_task_number(8)
        previewed = self.service.preview(ticket)["prompt"]
        self.service.work(ticket)
        launched = self.spawn.calls[0]["argv"][-1]
        self.assertEqual(_block(previewed), _block(launched))
        self.assertIn("REVIEW: rejected", _block(launched))

    def test_the_board_is_read_once_for_a_preview(self):
        reads = [
            call for call in self.vikunja.calls
            if call[0] == "GET" and call[1].endswith("/comments")
        ]
        self.service.preview(self.service.get_by_task_number(8))
        after = [
            call for call in self.vikunja.calls
            if call[0] == "GET" and call[1].endswith("/comments")
        ]
        self.assertEqual(len(after) - len(reads), 1)


class BothExecutorsGetTheSameComments(ServiceTestCase):
    """The prompt is harness-neutral, and the comments are part of it.

    Nothing is trimmed for being a local model: the local executor's brief is
    the same text, which is the property task 810 established and this must not
    quietly undo by assembling the context per harness.
    """

    comments = TASK_813

    def setUp(self) -> None:
        super().setUp()
        write_repo(self.workdir)
        self.config = make_config(
            self.state_dir, workdir=self.workdir, local_executor_base_url=BASE_URL
        )
        self.service.config = self.config
        self.launcher.config = self.config

    def test_the_local_run_and_the_default_run_get_the_same_comment_block(self):
        ticket = self.service.get_by_task_number(8)
        self.service.work(ticket, LOCAL_EXECUTOR)
        self.service.work(self.service.get_by_task_number(8))
        local, default = (call["argv"][-1] for call in self.spawn.calls)
        self.assertEqual(_block(local), _block(default))
        self.assertIn("REVIEW: rejected", _block(local))


def _block(prompt: str) -> str:
    """The comment section of a prompt, so two prompts can be compared on it
    alone — they legitimately differ on the worktree path they name."""
    start = prompt.index(BEGIN)
    return prompt[start : prompt.index(END, start) + len(END)]


if __name__ == "__main__":
    unittest.main()
