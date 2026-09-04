"""Reading what a run is doing, from what a launch already wrote (task 751).

`start_task_run` could set a run going and the board would show In Progress,
and then there was nothing to ask. A run still thinking, a run in its tests, a
run in its closing report and a run whose process died an hour ago all looked
identical from outside: one column, no comment. Task 714 spent eight minutes in
that state and the only way to tell which it was involved reading files on the
host.

Nothing new is recorded to answer it. A launch already writes three artifacts —
a lock naming the process, an append-only log of launches and endings, and the
run's own output — and this reads those three together. So the interesting
tests here are about the *combinations*, because the states are distinguished
by what is missing:

* a lock with a live PID is a run that is **running**;
* an ending recorded against that launch says **finished** or **failed**;
* a launch with no ending and no live process is **lost**, and that is the case
  worth naming. It is what a restart of the launcher service leaves behind:
  the run dies with the service's control group and the thread that would have
  recorded its ending dies with the service too. Calling it `finished` would
  claim an exit nobody saw; calling it `failed` would claim a failure the
  runner never observed. It is neither, and a ticket sitting In Progress with
  nothing reported on it is what it looks like from the board;
* the same run once the runner has closed it out is **reconciled** (task 757).
  Still no outcome — the recorded ending says only that one was missed — but
  the lock is released, the board has been told and nothing further will
  happen, which is a different thing for a reader to do about it.

And two properties that are not about states at all: the read must not change
what it reads (a stale lock is the evidence, and reclaiming it is what the
neighbouring reads do), and the run's output is bounded and carries no token.
"""

from __future__ import annotations

import json
from pathlib import Path

from vikunja_claude.launcher import EVENT_ORPHANED
from vikunja_claude.run_output import (
    STATUS_READ_BYTES,
    STATUS_TAIL_BYTES,
    STATUS_TAIL_LINES,
)

from .support import TOKEN, ServiceTestCase

#: The fixture's Ready ticket: board #8, row id 9. Both numbers are used here —
#: the status is asked for by row id, the way the runner addresses everything,
#: and answers with the board number, which is what a person quotes.
READY = 8
READY_ROW_ID = 9
LAUNCHED_PID = 4242


class RunStatusTestCase(ServiceTestCase):
    """A launched run whose liveness and ending the test controls."""

    def launch(self, task_number: int = READY) -> dict:
        return self.service.work(self.service.get_by_task_number(task_number))

    def status(self, task_id: int = READY_ROW_ID) -> dict:
        return self.launcher.run_status(task_id)

    def end(self, event: str = "finished", pid: int = LAUNCHED_PID, **fields):
        """Record an ending the way the reaper would have, had it survived."""
        self.launcher._log(event, reference=f"#{READY}", pid=pid, **fields)

    def log_file(self, task_id: int = READY_ROW_ID):
        launched = [
            json.loads(line)
            for line in self.config.log_path.read_text(encoding="utf-8").splitlines()
        ]
        for record in reversed(launched):
            if record.get("event") == "launched":
                return Path(record["log_file"])
        raise AssertionError("nothing was launched")


class TestTheStateOfARun(RunStatusTestCase):
    def test_a_live_process_is_running(self):
        self.launch()
        self.alive_pids.add(LAUNCHED_PID)

        status = self.status()
        self.assertEqual(status["state"], "running")
        self.assertTrue(status["alive"])
        self.assertIsNone(status["finished_at"])

    def test_a_clean_exit_is_finished(self):
        self.launch()
        self.end("finished", exit_status=0)

        status = self.status()
        self.assertEqual(status["state"], "finished")
        self.assertFalse(status["alive"])
        self.assertEqual(status["exit_status"], 0)
        self.assertIsNotNone(status["finished_at"])

    def test_a_non_zero_exit_is_failed(self):
        self.launch()
        self.end("finished", exit_status=1)

        status = self.status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["exit_status"], 1)
        self.assertFalse(status["timed_out"])

    def test_a_run_killed_for_running_too_long_is_failed_and_says_so(self):
        """The timeout is logged and *then* a `finished` with no exit status.

        Read on its own, that finished record is a run with an unknown exit —
        which is also what a crash looks like. The two are separated by the
        timeout record, so both are read.
        """
        self.launch()
        # A ceiling an operator configured. There is no default one (task 817),
        # so this number is an example rather than the lifetime of a run — it
        # used to be 10800 here, which read as "what every run gets".
        self.end("timeout", after_seconds=900)
        self.end("finished", exit_status=None)

        status = self.status()
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status["timed_out"])

    def test_a_launch_with_no_ending_and_no_process_is_lost(self):
        """Task 714's actual state, and the reason this tool exists.

        Nothing here says the run failed, because nothing observed it fail. It
        started, it is gone, and the runner never saw how it ended.
        """
        self.launch()

        status = self.status()
        self.assertEqual(status["state"], "lost")
        self.assertFalse(status["alive"])
        self.assertIsNone(status["finished_at"])
        self.assertIsNone(status["exit_status"])

    def test_a_recorded_missed_ending_is_reconciled_not_lost(self):
        """The one word used to cover both, so a run that had been closed out
        was reported as one nothing had been done about (task 757).

        What it must NOT gain is an outcome: the record says an ending was
        missed, so there is still no exit status and still no timeout.
        """
        self.launch()
        self.end(EVENT_ORPHANED)

        status = self.status()
        self.assertEqual(status["state"], "reconciled")
        self.assertFalse(status["alive"])
        self.assertIsNotNone(status["reconciled_at"])
        self.assertIsNone(status["finished_at"])
        self.assertIsNone(status["exit_status"])
        self.assertFalse(status["timed_out"])

    def test_an_observed_ending_beats_a_recorded_one(self):
        """They should never both exist — reconciliation refuses to write over
        an ending that was seen — but the one somebody watched is the true
        account, so the split must not put `reconciled` above it."""
        self.launch()
        self.end("finished", exit_status=0)
        self.end(EVENT_ORPHANED)

        self.assertEqual(self.status()["state"], "finished")

    def test_a_task_that_was_never_run_is_none(self):
        status = self.status(task_id=999999)
        self.assertEqual(status["state"], "none")
        self.assertFalse(status["alive"])
        self.assertIsNone(status["started_at"])
        self.assertEqual(status["output_tail"], [])

    def test_the_run_is_named_by_its_board_number(self):
        self.launch()
        status = self.status()
        self.assertEqual(status["number"], READY)
        self.assertEqual(status["reference"], f"#{READY}")

    def test_the_executor_and_model_come_back_as_the_launch_recorded_them(self):
        self.launch()
        status = self.status()
        self.assertEqual(status["executor"], "claude")
        self.assertIsNone(status["model"])


class TestWhichRunIsReported(RunStatusTestCase):
    def test_the_latest_launch_wins_when_a_task_has_run_twice(self):
        """A finished run then a fresh one: the fresh one is the answer.

        Matching on the earliest launch would report a task as `finished`
        while a second run was working on it right now.
        """
        self.launch()
        self.end("finished", exit_status=0)
        self.launcher._release(READY_ROW_ID)
        self.launch()
        self.alive_pids.add(LAUNCHED_PID)

        self.assertEqual(self.status()["state"], "running")

    def test_another_tasks_run_is_not_this_tasks_run(self):
        """The launch log is one file for every task, so a status that matched
        loosely would answer with whatever ran last on the host."""
        self.launch()
        self.end("finished", exit_status=0)

        self.assertEqual(self.status(task_id=999999)["state"], "none")


class TestTheReadChangesNothing(RunStatusTestCase):
    def test_a_stale_lock_survives_being_read(self):
        """`active_launch()` reclaims a lock whose PID is gone, which is right
        when something is about to launch. Here it would delete the evidence
        that a run was left un-reaped — the read would erase what it reports."""
        self.launch()
        lock = self.config.lock_dir / f"task-{READY_ROW_ID}.json"
        self.assertTrue(lock.exists())

        self.assertEqual(self.status()["state"], "lost")

        self.assertTrue(lock.exists(), "the status read reclaimed the lock")
        self.assertEqual(self.status()["state"], "lost")

    def test_reading_writes_nothing_to_the_launch_log(self):
        self.launch()
        before = self.config.log_path.read_text(encoding="utf-8")

        self.status()
        self.status()

        self.assertEqual(self.config.log_path.read_text(encoding="utf-8"), before)

    def test_reading_starts_nothing(self):
        self.launch()
        self.status()
        self.assertEqual(len(self.spawn.calls), 1)


class TestTheOutputItHandsBack(RunStatusTestCase):
    def test_it_returns_the_end_of_what_the_run_wrote(self):
        self.launch()
        self.log_file().write_text("first\nsecond\nthird\n", encoding="utf-8")

        status = self.status()
        self.assertEqual(status["output_tail"], ["first", "second", "third"])
        self.assertFalse(status["output_truncated"])
        self.assertIsNotNone(status["output_at"])

    def test_it_is_bounded_by_lines(self):
        self.launch()
        self.log_file().write_text(
            "".join(f"line {n}\n" for n in range(STATUS_TAIL_LINES * 3)),
            encoding="utf-8",
        )

        status = self.status()
        self.assertEqual(len(status["output_tail"]), STATUS_TAIL_LINES)
        self.assertTrue(status["output_truncated"])
        # The END of the output, which is where a run says what it is doing now.
        self.assertEqual(
            status["output_tail"][-1], f"line {STATUS_TAIL_LINES * 3 - 1}"
        )

    def test_it_is_bounded_by_bytes_when_the_lines_are_long(self):
        """A line cap alone is not a bound: forty lines of a megabyte each is
        forty megabytes pulled through a read surface."""
        self.launch()
        self.log_file().write_text(
            "".join("x" * 4000 + "\n" for _ in range(10)), encoding="utf-8"
        )

        status = self.status()
        self.assertLess(len(status["output_tail"]), STATUS_TAIL_LINES)
        self.assertLessEqual(
            sum(len(line) for line in status["output_tail"]), STATUS_TAIL_BYTES
        )
        self.assertTrue(status["output_truncated"])

    def test_a_line_cut_in_half_by_the_read_window_is_dropped(self):
        """A byte seek lands mid-line. Publishing that fragment would show a
        line the run never wrote, beginning where the reader happened to land.

        Built from `STATUS_READ_BYTES`, which is how far back the reader looks,
        and no longer from the bound on what it returns: those became two
        numbers when a single stream-json event turned out to be bigger than
        the whole of an answer (task 755).
        """
        self.launch()
        self.log_file().write_text(
            "PREFIX" + "y" * STATUS_READ_BYTES + "\ncomplete line\n",
            encoding="utf-8",
        )

        status = self.status()
        self.assertEqual(status["output_tail"], ["complete line"])

    def test_the_vikunja_token_never_travels_out_in_the_output(self):
        """The run holds it in its environment, so a traceback or a verbose
        HTTP log can put it in the file this reads."""
        self.launch()
        self.log_file().write_text(
            f"Authorization: Bearer {TOKEN}\nnext line\n", encoding="utf-8"
        )

        status = self.status()
        self.assertNotIn(TOKEN, "\n".join(status["output_tail"]))
        self.assertIn("[redacted]", status["output_tail"][0])
        self.assertEqual(status["output_tail"][1], "next line")

    def test_a_missing_log_file_is_empty_output_not_an_error(self):
        """A run whose log was cleared away is still a run with a state."""
        self.launch()
        self.log_file().unlink()

        status = self.status()
        self.assertEqual(status["state"], "lost")
        self.assertEqual(status["output_tail"], [])
        self.assertIsNone(status["output_at"])
