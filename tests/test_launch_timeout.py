"""What the reaper does when a run outstays the time limit (task 758).

The timeout path used to send SIGTERM to the run's process group, set the exit
status to None, release the ticket's lock and log `finished`. Every one of those
words was written without waiting for anything: `finished` meant *a signal was
sent*, not that a process had been reaped. A child that does not die on SIGTERM
— Claude Code inside a long tool call is the realistic case — went on running in
the ticket's worktree with the lock gone, so nothing recorded that it was there
and the same ticket could be launched straight back into the directory it was
still editing.

So the tests here are about the two orderings, and they use a **real child that
really ignores SIGTERM**, because a fake that returns from `wait()` cannot be
the thing that was wrong:

* the lock outlives the signal — it is released when the process is dead, not
  when it has been asked to die;
* SIGTERM is escalated to SIGKILL within a bounded grace, and what that
  escalation achieved is what gets logged;
* a run that survives even that keeps its lock, is reported as `unkillable`
  rather than as a run that is working, and cannot be relaunched;
* and no exit status is invented anywhere along the way. A killed run has none.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vikunja_claude.launcher import AlreadyRunning, Launcher, pid_alive
from vikunja_claude.vikunja import Ticket

from .support import make_config, make_repo

#: The child. It is handed the prompt as its last argument, exactly as Claude
#: Code is, and the test uses that argument to tell it where to leave its
#: markers: one when its SIGTERM handler is installed, one when that handler
#: fires. The second is what proves the signal was delivered and survived — a
#: child that merely outlived SIGTERM might never have been sent it.
CHILD = """
import signal, sys, time
marker = sys.argv[1]
signal.signal(signal.SIGTERM, lambda *_: open(marker + '.term', 'w').close())
open(marker + '.ready', 'w').close()
time.sleep(120)
"""

TICKET = Ticket(
    task_id=9,
    title="A timed-out run is recorded as finished without confirming it died",
    description_html="",
    bucket_id=1,
    bucket_title="Ready",
    done=False,
    created="2026-09-01T20:09:43Z",
    task_number=758,
)


def wait_for(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


class TimeoutTestCase(unittest.TestCase):
    """A launcher whose time limit is already spent when the run starts."""

    #: Per signal, so a wedged child costs at most twice this. Small here and
    #: long in `TheLockOutlivesTheSignal`, where the point is to look at the
    #: run *during* the grace.
    grace = 0.5

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.markers = self.state_dir / "child"
        self.config = make_config(
            self.state_dir,
            workdir=make_repo(self.state_dir / "repo"),
            claude_bin=sys.executable,
            claude_args=["-c", CHILD],
            # Spent before the run begins, so the reaper reaches its timeout on
            # the first wait rather than the test waiting out a real one.
            launch_timeout_seconds=0,
            kill_grace_seconds=self.grace,
        )
        self.launcher = Launcher(self.config, spawn=self._spawn_when_ready)
        self.spawned: list[int] = []
        self.addCleanup(self._stop_everything)

    def _spawn_when_ready(self, argv, **kwargs) -> subprocess.Popen:
        """A real Popen, held until the child has installed its handler.

        The reaper thread starts the moment this returns, and its time limit is
        already spent, so returning earlier would race the signal against the
        handler and sometimes kill a child that had not yet refused anything.
        """
        process = subprocess.Popen(argv, **kwargs)
        self.spawned.append(process.pid)
        if not wait_for(Path(f"{self.markers}.ready")):
            raise AssertionError("the child never started")
        return process

    def _stop_everything(self) -> None:
        """Kill what is left, then wait for the reaper that is watching it.

        Both halves, in that order. A test that deliberately leaves a child
        alive also leaves a reaper thread inside its grace, and that thread
        writes the ending and unlinks the lock — into a state directory the
        cleanup is otherwise removing underneath it.
        """
        for pid in self.spawned:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for thread in threading.enumerate():
            if thread.name.startswith("reaper-task-"):
                thread.join(timeout=30)

    # -- reading what the run left behind ----------------------------------

    def launch(self):
        return self.launcher.launch(TICKET, lambda workdir: str(self.markers))

    def events(self) -> list[dict]:
        try:
            text = self.config.log_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in text.splitlines()]

    def event(self, name: str) -> dict | None:
        return next((r for r in self.events() if r.get("event") == name), None)

    def locked(self) -> bool:
        return self.launcher._lock_path(TICKET.task_id).exists()

    def join_reaper(self) -> None:
        for thread in threading.enumerate():
            if thread.name == f"reaper-task-{TICKET.task_id}":
                thread.join(timeout=30)
                self.assertFalse(thread.is_alive(), "the reaper never returned")


class Escalation(TimeoutTestCase):
    def test_a_child_that_ignores_sigterm_is_killed_within_the_grace(self):
        record = self.launch()
        self.join_reaper()

        self.assertTrue(
            Path(f"{self.markers}.term").exists(),
            "the child never saw SIGTERM, so nothing here was escalated",
        )
        self.assertFalse(pid_alive(record.pid))

    def test_what_the_escalation_achieved_is_what_is_logged(self):
        """Both fields are observations, not intentions: one says SIGKILL was
        needed, the other that the process is actually gone."""
        self.launch()
        self.join_reaper()

        timeout = self.event("timeout")
        self.assertEqual(timeout["after_seconds"], 0)
        self.assertTrue(timeout["escalated"])
        self.assertTrue(timeout["terminated"])

    def test_the_killed_run_is_finished_with_no_exit_status(self):
        """It has none. The status `wait()` returns after a SIGKILL is the
        signal the runner sent, not an outcome the run reached."""
        self.launch()
        self.join_reaper()

        finished = self.event("finished")
        self.assertIsNone(finished["exit_status"])
        self.assertTrue(self.event("timeout")["terminated"])

    def test_the_lock_is_released_only_once_the_process_is_dead(self):
        self.launch()
        self.join_reaper()

        self.assertFalse(self.locked())
        self.assertEqual(self.launcher.running(), [])

    def test_the_ending_is_recorded_before_the_lock_goes(self):
        """The ordering reconciliation already uses: a crash between the two
        leaves a lock, which the next read reclaims, rather than a released
        lock whose ending nothing ever wrote."""
        released_while = []
        real_release = self.launcher._release
        self.launcher._release = lambda task_id: (
            released_while.append([r["event"] for r in self.events()]),
            real_release(task_id),
        )

        self.launch()
        self.join_reaper()

        self.assertEqual(len(released_while), 1)
        self.assertIn("finished", released_while[0])

    def test_the_run_reads_as_failed_for_running_too_long(self):
        self.launch()
        self.join_reaper()

        status = self.launcher.run_status(TICKET.task_id)
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status["timed_out"])
        self.assertIsNone(status["exit_status"])


class TheLockOutlivesTheSignal(TimeoutTestCase):
    """The grace is long here, so the run can be looked at inside it.

    This is the window the defect turned into a released lock: SIGTERM sent, the
    child still very much alive, and — before this — nothing recording that.
    """

    grace = 30.0

    def signalled(self):
        record = self.launch()
        self.assertTrue(
            wait_for(Path(f"{self.markers}.term")),
            "the child was never sent SIGTERM",
        )
        return record

    def test_the_lock_is_still_held_while_the_child_ignores_the_signal(self):
        record = self.signalled()

        self.assertTrue(pid_alive(record.pid))
        self.assertTrue(self.locked())
        self.assertIsNone(self.event("finished"))

    def test_the_ticket_cannot_be_relaunched_into_the_same_worktree(self):
        """The whole point of holding the lock. Released on the signal, this
        second launch succeeded, into the directory the first run was still
        editing."""
        record = self.signalled()

        with self.assertRaises(AlreadyRunning) as refusal:
            self.launch()
        self.assertEqual(refusal.exception.pid, record.pid)

    def test_nothing_is_concluded_about_the_run_until_it_is_over(self):
        """Mid-escalation it is still `running`, because that is still true and
        no ending has been observed. The timeout is logged with what it
        achieved, which is not known yet."""
        self.signalled()

        status = self.launcher.run_status(TICKET.task_id)
        self.assertEqual(status["state"], "running")
        self.assertTrue(status["alive"])
        self.assertIsNone(self.event("timeout"))

    def test_the_lock_goes_when_the_process_finally_does(self):
        record = self.signalled()

        os.killpg(os.getpgid(record.pid), signal.SIGKILL)
        self.join_reaper()

        self.assertFalse(self.locked())
        self.assertIsNone(self.event("finished")["exit_status"])
        # It died inside the first grace, so SIGKILL was never the runner's.
        self.assertFalse(self.event("timeout")["escalated"])
        self.assertTrue(self.event("timeout")["terminated"])


class StubbornProcess:
    """A process that outlives everything, which no real child can be asked to.

    SIGKILL cannot be caught, so the one case left — a child wedged in the
    kernel, which is what a dead mount or a stuck device gives you — is
    unreachable from a test with a real process. The signals are stubbed out
    with it, so nothing here can reach a real process group.
    """

    def __init__(self, pid: int = 424242):
        self.pid = pid

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("claude", timeout)


class AProcessThatSurvivesSigkill(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.config = make_config(
            self.state_dir,
            workdir=make_repo(self.state_dir / "repo"),
            launch_timeout_seconds=0,
            kill_grace_seconds=0.01,
        )
        self.process = StubbornProcess()
        self.alive = {self.process.pid}
        self.launcher = Launcher(
            self.config,
            spawn=lambda argv, **kwargs: self.process,
            is_alive=lambda pid: pid in self.alive,
            reap=False,
        )
        self.signals: list[int] = []
        patch = mock.patch.multiple(
            "vikunja_claude.launcher.os",
            getpgid=lambda pid: pid,
            killpg=lambda pgid, number: self.signals.append(number),
        )
        patch.start()
        self.addCleanup(patch.stop)

        self.record = self.launcher.launch(TICKET, lambda workdir: "prompt")
        self.launcher._wait_and_log(
            TICKET.task_id, TICKET.board_reference, self.process
        )

    def events(self) -> list[str]:
        text = self.config.log_path.read_text(encoding="utf-8")
        return [json.loads(line)["event"] for line in text.splitlines()]

    def test_it_was_escalated_before_being_given_up_on(self):
        self.assertEqual(self.signals, [signal.SIGTERM, signal.SIGKILL])

    def test_the_condition_is_recorded_and_the_run_is_not_called_finished(self):
        self.assertNotIn("finished", self.events())
        timeout = json.loads(
            self.config.log_path.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(timeout["event"], "timeout")
        self.assertTrue(timeout["escalated"])
        self.assertFalse(timeout["terminated"])

    def test_it_keeps_its_lock_and_the_ticket_cannot_be_relaunched(self):
        self.assertTrue(self.launcher._lock_path(TICKET.task_id).exists())
        with self.assertRaises(AlreadyRunning):
            self.launcher.launch(TICKET, lambda workdir: "prompt")

    def test_it_is_reported_as_unkillable_rather_than_as_working(self):
        """`running` would send a reader away to wait for a report from a
        process that has been told to stop twice and will never write one."""
        status = self.launcher.run_status(TICKET.task_id)
        self.assertEqual(status["state"], "unkillable")
        self.assertTrue(status["alive"])
        self.assertTrue(status["timed_out"])
        self.assertIsNone(status["exit_status"])
        self.assertIsNone(status["finished_at"])

    def test_once_it_finally_dies_the_run_is_closed_out_like_any_other(self):
        """No second reaper and no sweep: the reclaim that already happens on
        any read of the lock is what ends this, and it still invents nothing —
        the ending was missed, so the run is `reconciled`, not `finished`."""
        self.alive.clear()

        self.assertIsNone(self.launcher.active_launch(TICKET.task_id))
        status = self.launcher.run_status(TICKET.task_id)
        self.assertEqual(status["state"], "reconciled")
        self.assertIsNone(status["exit_status"])
        self.assertIsNotNone(status["reconciled_at"])


if __name__ == "__main__":
    unittest.main()
