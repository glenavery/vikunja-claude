"""Launching: bucket move, token handling, and one run per ticket."""

from __future__ import annotations

import json
import unittest

from vikunja_claude.launcher import AlreadyRunning, LaunchError

from pathlib import Path

from .support import TOKEN, ServiceTestCase


class Launching(ServiceTestCase):
    def test_moves_the_ticket_to_in_progress_before_launching(self):
        self.service.work(self.service.get_by_task_number(8))
        self.assertEqual(self.vikunja.bucket_of(9), "In Progress")
        self.assertEqual(len(self.spawn.calls), 1)

    def test_reports_the_move_and_the_pid(self):
        result = self.service.work(self.service.get_by_task_number(8))
        self.assertTrue(result["launched"])
        self.assertEqual(result["moved_to"], "In Progress")
        self.assertEqual(result["number"], 8)
        self.assertEqual(result["pid"], 4242)

    def test_a_ticket_already_in_progress_is_not_moved_again(self):
        result = self.service.work(self.service.get_by_task_number(10))
        self.assertIsNone(result["moved_to"])
        move_calls = [c for c in self.vikunja.calls if c[0] == "POST"]
        self.assertEqual(move_calls, [])

    def test_runs_claude_in_a_worktree_of_the_configured_repository(self):
        """Not the repository root — that was task 756's whole defect."""
        self.service.work(self.service.get_by_task_number(8))
        cwd = Path(self.spawn.calls[0]["cwd"])
        self.assertEqual(cwd, self.workdir / ".claude" / "worktrees" / "task-8")
        self.assertNotEqual(cwd, self.workdir)

    def test_prompt_is_the_last_argument_and_token_is_not_on_the_command_line(self):
        self.service.work(self.service.get_by_task_number(8))
        argv = self.spawn.calls[0]["argv"]
        self.assertEqual(argv[0], "claude")
        self.assertIn("TICKET #8", argv[-1])
        self.assertFalse(any(TOKEN in str(arg) for arg in argv))

    def test_token_reaches_the_child_through_the_environment_only(self):
        self.service.work(self.service.get_by_task_number(8))
        env = self.spawn.calls[0]["env"]
        self.assertEqual(env["VIKUNJA_API_TOKEN"], TOKEN)
        self.assertEqual(env["VIKUNJA_TASK_NUMBER"], "8")

    def test_launch_is_logged_with_ticket_pid_and_time(self):
        self.service.work(self.service.get_by_task_number(8))
        entries = [
            json.loads(line)
            for line in self.config.log_path.read_text().splitlines()
        ]
        launched = [e for e in entries if e["event"] == "launched"]
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0]["reference"], "#8")
        self.assertEqual(launched[0]["pid"], 4242)
        self.assertIn("at", launched[0])


class DuplicateLaunchPrevention(ServiceTestCase):
    def test_second_launch_while_running_is_refused(self):
        self.service.work(self.service.get_by_task_number(8))
        self.alive_pids.add(4242)

        with self.assertRaises(AlreadyRunning) as caught:
            self.service.work(self.service.get_by_task_number(8))
        self.assertIn("already working #8", str(caught.exception))
        self.assertNotIn("task 9", str(caught.exception))

    def test_refusal_does_not_spawn_a_second_process(self):
        self.service.work(self.service.get_by_task_number(8))
        self.alive_pids.add(4242)
        with self.assertRaises(AlreadyRunning):
            self.service.work(self.service.get_by_task_number(8))
        self.assertEqual(len(self.spawn.calls), 1)

    def test_a_different_ticket_can_run_concurrently(self):
        self.service.work(self.service.get_by_task_number(8))
        self.alive_pids.add(4242)
        self.service.work(self.service.get_by_task_number(9))
        self.assertEqual(len(self.spawn.calls), 2)

    def test_a_dead_pid_leaves_a_stale_lock_that_is_reclaimed(self):
        self.service.work(self.service.get_by_task_number(8))
        # 4242 was never added to alive_pids, so the lock is stale.
        self.service.work(self.service.get_by_task_number(8))
        self.assertEqual(len(self.spawn.calls), 2)

    def test_lock_survives_a_new_launcher_instance(self):
        from vikunja_claude.launcher import Launcher

        self.service.work(self.service.get_by_task_number(8))
        self.alive_pids.add(4242)

        fresh = Launcher(
            self.config,
            spawn=self.spawn,
            is_alive=lambda pid: pid in self.alive_pids,
            reap=False,
        )
        self.assertIsNotNone(fresh.active_launch(9))
        with self.assertRaises(AlreadyRunning):
            fresh.launch(self.service.get_by_task_number(8), "prompt")

    def test_running_lists_a_live_launch(self):
        self.alive_pids.add(4242)
        self.service.work(self.service.get_by_task_number(8))
        self.assertEqual([r["number"] for r in self.launcher.running()], [8])

    def test_running_drops_and_clears_a_dead_launch(self):
        self.service.work(self.service.get_by_task_number(8))  # pid 4242 never marked alive
        self.assertEqual(self.launcher.running(), [])
        self.assertFalse((self.config.lock_dir / "task-9.json").exists())


class LaunchFailure(ServiceTestCase):
    def test_missing_claude_binary_is_reported_and_unlocks_the_ticket(self):
        self.spawn.error = FileNotFoundError("no such file: claude")
        with self.assertRaises(LaunchError):
            self.service.work(self.service.get_by_task_number(8))
        self.assertIsNone(self.launcher.active_launch(9))

    def test_failed_launch_is_logged(self):
        self.spawn.error = FileNotFoundError("no such file: claude")
        with self.assertRaises(LaunchError):
            self.service.work(self.service.get_by_task_number(8))
        entries = [
            json.loads(line)
            for line in self.config.log_path.read_text().splitlines()
        ]
        self.assertEqual([e["event"] for e in entries], ["launch_failed"])


if __name__ == "__main__":
    unittest.main()
