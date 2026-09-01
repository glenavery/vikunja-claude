"""Closing out a run this service lost track of (task 754).

Stopping `vikunja-claude.service` destroys two things at once. The launched
Claude Code process sits in the unit's control group, so systemd kills it — and
it is the only thing that comments on the board or moves the ticket out of In
Progress. The thread that would have recorded the ending lives in the service
process, so it dies too. The result was a ticket In Progress forever, a lock
file claiming a dead PID was running, and a launch log with an opening record
and no closing one. Task #714 sat in exactly that state; task 751 made it
*visible* as `lost` and this makes it *end*.

Two properties carry the design, and the tests are mostly about them:

* **A recorded ending is not an observed one.** Reconciliation writes
  `orphaned`, never `finished` and never `timeout`, because it did not watch
  the process and has no exit status to report. Recording either would invent
  the outcome this exists to stop inventing. `lost` therefore stays `lost`
  after reconciliation — what changes is that it becomes a closed, dated fact
  instead of an open-ended inference from a missing record.
* **The local half and the board half fail differently.** The ending is
  recorded and the lock released whatever the board does, because a Vikunja
  that is down must not stop the service starting or leave a lock claiming a
  live run. But a board that was never told is the original bug in miniature,
  so the result says whether it was.

The restart itself is simulated the way it actually happens: a second
`TicketService` over the same state directory, with the first one's child no
longer alive. Asserting the reconcile helper on its own would pass without any
of it ever running at startup.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from vikunja_claude.launcher import EVENT_ORPHANED, Launcher
from vikunja_claude.service import TicketService

from .support import ServiceTestCase

READY_TICKET = 8
READY_ROW_ID = 9
LAUNCHED_PID = 4242


class RestartTestCase(ServiceTestCase):
    """A launched run, and the means to restart the service under it."""

    def launch(self, task_number: int = READY_TICKET):
        return self.service.work(self.service.get_by_task_number(task_number))

    def restart(self) -> TicketService:
        """What a restart is: a new service over the same state directory.

        The child is not alive in the new instance — `alive_pids` is empty
        unless a test says otherwise — which is the situation systemd leaves
        behind when it kills the control group.
        """
        launcher = Launcher(
            self.config,
            spawn=self.spawn,
            is_alive=lambda pid: pid in self.alive_pids,
            reap=False,
        )
        return TicketService(self.config, self.client, launcher)

    def events(self, kind: str | None = None) -> list[dict]:
        try:
            lines = self.config.log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records = [json.loads(line) for line in lines]
        return [r for r in records if kind is None or r.get("event") == kind]

    def lock_path(self, task_id: int = READY_ROW_ID) -> Path:
        return self.config.lock_dir / f"task-{task_id}.json"

    def comments_on(self, task_id: int = READY_ROW_ID) -> list[dict]:
        return self.vikunja.comments.get(task_id, [])


class TestARestartClosesOutTheRunItKilled(RestartTestCase):
    def test_the_ending_the_run_never_got_is_recorded(self):
        self.launch()
        self.assertEqual(self.events(EVENT_ORPHANED), [])

        self.restart().reconcile_orphaned_runs()

        orphaned = self.events(EVENT_ORPHANED)
        self.assertEqual(len(orphaned), 1)
        self.assertEqual(orphaned[0]["reference"], f"#{READY_TICKET}")
        self.assertEqual(orphaned[0]["pid"], LAUNCHED_PID)
        self.assertIn("at", orphaned[0])

    def test_the_lock_stops_claiming_a_dead_process_is_running(self):
        self.launch()
        self.assertTrue(self.lock_path().exists())

        self.restart().reconcile_orphaned_runs()

        self.assertFalse(self.lock_path().exists())

    def test_the_ending_is_recorded_before_the_lock_is_released(self):
        """A crash between the two must leave the lock, which is reconcilable
        again — not a released lock whose ending nothing will ever revisit."""
        self.launch()
        service = self.restart()
        seen = {}
        original = service.launcher._release

        def watching_release(task_id):
            seen["orphan_written"] = bool(self.events(EVENT_ORPHANED))
            return original(task_id)

        service.launcher._release = watching_release
        service.reconcile_orphaned_runs()

        self.assertTrue(seen["orphan_written"])

    def test_the_board_is_told_and_the_ticket_stops_saying_in_progress(self):
        self.launch()
        self.assertEqual(self.vikunja.bucket_of(READY_ROW_ID), "In Progress")

        results = self.restart().reconcile_orphaned_runs()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["reported"])
        self.assertEqual(len(self.comments_on()), 1)
        self.assertEqual(self.vikunja.bucket_of(READY_ROW_ID), "Ready")

    def test_the_comment_claims_neither_success_nor_failure(self):
        """Nothing observed how the run ended, so the comment may not say. It
        reports that the run is gone and that anything it did is unverified."""
        self.launch()
        self.restart().reconcile_orphaned_runs()

        text = self.comments_on()[0]["comment"].lower()
        self.assertIn("lost", text)
        self.assertIn("unverified", text)
        self.assertNotIn("failed", text)
        self.assertNotIn("completed", text)
        self.assertNotIn("succeeded", text)

    def test_the_status_read_now_shows_a_closed_ending(self):
        """`lost` stays `lost` — reconciliation records the ending, it does not
        discover what the ending was. What changes is that it has a time."""
        self.launch()
        before = self.restart().launcher.run_status(READY_ROW_ID)
        self.assertEqual(before["state"], "lost")
        self.assertIsNone(before["reconciled_at"])

        service = self.restart()
        service.reconcile_orphaned_runs()
        after = service.launcher.run_status(READY_ROW_ID)

        self.assertEqual(after["state"], "lost")
        self.assertIsNotNone(after["reconciled_at"])
        # Still no exit status, and still not a timeout: neither was observed.
        self.assertIsNone(after["exit_status"])
        self.assertFalse(after["timed_out"])


class TestWhatReconciliationLeavesAlone(RestartTestCase):
    def test_a_run_that_is_still_working_is_untouched(self):
        """The point is to close what ended, never to disturb what is running.
        A restart that reconciled a live run would be worse than the bug."""
        self.launch()
        self.alive_pids.add(LAUNCHED_PID)

        results = self.restart().reconcile_orphaned_runs()

        self.assertEqual(results, [])
        self.assertEqual(self.events(EVENT_ORPHANED), [])
        self.assertTrue(self.lock_path().exists())
        self.assertEqual(self.comments_on(), [])
        self.assertEqual(self.vikunja.bucket_of(READY_ROW_ID), "In Progress")

    def test_a_run_that_ended_properly_is_never_orphaned(self):
        """A lock can outlive a reaped run if the release failed. Reconciling
        that would write a second, contradictory ending over a clean exit."""
        self.launch()
        self.launcher._log(
            "finished", reference=f"#{READY_TICKET}", pid=LAUNCHED_PID,
            exit_status=0,
        )

        service = self.restart()
        results = service.reconcile_orphaned_runs()

        self.assertEqual(results, [])
        self.assertEqual(self.events(EVENT_ORPHANED), [])
        self.assertFalse(self.lock_path().exists())
        self.assertEqual(service.launcher.run_status(READY_ROW_ID)["state"],
                         "finished")

    def test_reconciling_twice_writes_one_ending_and_one_comment(self):
        """Restarting repeatedly must not comment repeatedly on a ticket whose
        run was already accounted for."""
        self.launch()
        self.restart().reconcile_orphaned_runs()
        second = self.restart().reconcile_orphaned_runs()

        self.assertEqual(second, [])
        self.assertEqual(len(self.events(EVENT_ORPHANED)), 1)
        self.assertEqual(len(self.comments_on()), 1)

    def test_a_lock_that_outlived_its_own_orphan_record_is_not_recorded_twice(self):
        """The branch the twice-reconciled test does NOT reach.

        After a clean reconciliation the lock is gone, so a second pass finds
        nothing and the already-orphaned guard never fires. It fires when the
        ending was recorded and the release did not happen — a crash between
        the two, or a failed unlink — and without it that lock would collect a
        fresh, contradictory ending on every startup for as long as it sat
        there.

        No comment either: the run has already been accounted for once, and a
        second startup cannot tell whether the board was told, so it does not
        guess and say it twice.
        """
        self.launch()
        self.launcher._log(
            EVENT_ORPHANED, reference=f"#{READY_TICKET}", pid=LAUNCHED_PID,
            number=READY_TICKET,
        )
        self.assertTrue(self.lock_path().exists())

        results = self.restart().reconcile_orphaned_runs()

        self.assertEqual(results, [])
        self.assertEqual(len(self.events(EVENT_ORPHANED)), 1)
        self.assertFalse(self.lock_path().exists())
        self.assertEqual(self.comments_on(), [])

    def test_a_ticket_someone_already_moved_is_told_but_not_moved(self):
        """A human who moved it on knew more than this does, and undoing that
        would be a startup overruling a decision."""
        self.launch()
        project_id, view_id = self.service._ids()
        self.client.move_to_bucket(project_id, view_id, READY_ROW_ID, "Done")

        self.restart().reconcile_orphaned_runs()

        self.assertEqual(len(self.comments_on()), 1)
        self.assertEqual(self.vikunja.bucket_of(READY_ROW_ID), "Done")

    def test_the_comment_does_not_claim_a_move_that_did_not_happen(self):
        """Found live on #714, which was already out of In Progress: the
        comment said "moved back to Ready" of a ticket it had deliberately not
        moved. The move is conditional, so the sentence describing it has to be
        — otherwise the guard that protects a human's decision is undone in
        prose by the same call that honoured it."""
        self.launch()
        project_id, view_id = self.service._ids()
        self.client.move_to_bucket(project_id, view_id, READY_ROW_ID, "Done")

        self.restart().reconcile_orphaned_runs()

        text = self.comments_on()[0]["comment"]
        self.assertNotIn("moved back to Ready", text)
        self.assertIn("already moved it on", text)

    def test_the_comment_says_so_when_it_did_move_the_ticket(self):
        self.launch()
        self.restart().reconcile_orphaned_runs()

        self.assertIn("moved back to Ready", self.comments_on()[0]["comment"])

    def test_nothing_launched_means_nothing_to_reconcile(self):
        self.assertEqual(self.restart().reconcile_orphaned_runs(), [])
        self.assertEqual(self.events(EVENT_ORPHANED), [])


class TestABoardThatCannotBeReached(RestartTestCase):
    """The local half must not become conditional on the network half."""

    def test_the_ending_is_still_recorded_and_the_lock_still_released(self):
        self.launch()
        self.vikunja.fail = RuntimeError("vikunja is not up yet")

        results = self.restart().reconcile_orphaned_runs()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.events(EVENT_ORPHANED)), 1)
        self.assertFalse(self.lock_path().exists())

    def test_the_miss_is_reported_rather_than_passed_off_as_done(self):
        """A reconciliation that silently failed to report leaves exactly the
        condition this exists to end, with nothing left to notice it."""
        self.launch()
        self.vikunja.fail = RuntimeError("vikunja is not up yet")

        results = self.restart().reconcile_orphaned_runs()

        self.assertFalse(results[0]["reported"])
        failures = self.events("orphan_report_failed")
        self.assertEqual(len(failures), 1)
        self.assertIn("not up yet", failures[0]["error"])


class TestItRunsAtStartup(unittest.TestCase):
    """Wiring, not the helper. Asserting `reconcile_orphaned_runs` in isolation
    passes just as well on a service that never calls it."""

    def _config(self, tmp):
        from .support import make_config

        return make_config(Path(tmp), port=0)

    def test_building_the_server_reconciles_first(self):
        import tempfile

        from vikunja_claude import server

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                TicketService, "reconcile_orphaned_runs", return_value=[]
            ) as reconcile:
                built = server.build_server(self._config(tmp))
                built.server_close()

        reconcile.assert_called_once_with()

    def test_a_reconciliation_that_raises_does_not_stop_the_service(self):
        """It exists to make a bad startup visible. Refusing to start over it
        would turn one lost run into no runner at all."""
        import tempfile

        from vikunja_claude import server

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                TicketService,
                "reconcile_orphaned_runs",
                side_effect=RuntimeError("state directory is unreadable"),
            ):
                built = server.build_server(self._config(tmp))
                self.assertIsNotNone(built)
                built.server_close()
