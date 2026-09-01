"""A run works in a worktree of its own, made by the runner (task 756).

These drive the real launch path against a real git repository, with only the
spawn faked. That shape is deliberate: the defect being fixed was that nothing
in the runner created a worktree at all, so a test that asserted
`ensure_worktree` in isolation would have passed just as happily before the fix
as after it. What has to be proved is that a LAUNCH lands somewhere other than
the repository root.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vikunja_claude.launcher import LaunchError
from vikunja_claude.worktree import WorktreeError, ensure_worktree

from .fakes import task
from .support import ServiceTestCase, make_config, make_repo

READY_TICKET = 8
READY_ROW_ID = 9
#: The fake serves this one with no project-local index, which is the only case
#: that has no board number to name a worktree with.
NUMBERLESS_TASK = 91


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ALaunchLandsInItsOwnWorktree(ServiceTestCase):
    def work(self, number: int = READY_TICKET):
        return self.service.work(self.service.get_by_task_number(number))

    def test_the_child_is_spawned_in_the_worktree_not_the_checkout(self):
        self.work()
        cwd = Path(self.spawn.calls[0]["cwd"])
        self.assertEqual(cwd, self.workdir / ".claude" / "worktrees" / "task-8")
        self.assertNotEqual(cwd, self.workdir)
        self.assertTrue((cwd / ".git").exists())

    def test_the_worktree_is_on_its_own_branch_off_local_main(self):
        """Branched from local `main`, which is the freshness guarantee.

        `origin/main` is only as current as the last fetch, so branching from it
        would start runs from a base that ages silently between fetches.
        """
        self.work()
        worktree = Path(self.spawn.calls[0]["cwd"])
        self.assertEqual(git(worktree, "rev-parse", "--abbrev-ref", "HEAD"),
                         "worktree-task-8")
        self.assertEqual(
            git(worktree, "rev-parse", "HEAD"),
            git(self.workdir, "rev-parse", "main"),
        )

    def test_the_lock_the_log_and_the_status_all_name_the_worktree(self):
        """One directory, named the same way everywhere it is recorded.

        `LaunchRecord.workdir` feeds all three, so this is really asserting that
        nothing re-derives the path from config and disagrees.
        """
        result = self.work()
        worktree = str(self.workdir / ".claude" / "worktrees" / "task-8")

        self.assertEqual(result["workdir"], worktree)

        lock = json.loads(
            (self.config.lock_dir / f"task-{READY_ROW_ID}.json").read_text()
        )
        self.assertEqual(lock["workdir"], worktree)

        launched = [
            json.loads(line)
            for line in self.config.log_path.read_text().splitlines()
            if json.loads(line)["event"] == "launched"
        ]
        self.assertEqual(launched[-1]["workdir"], worktree)

    def test_the_prompt_names_the_worktree_and_its_branch(self):
        """The run is told where it is, and told not to leave.

        Without this the model has to infer the arrangement from its cwd, and
        the whole point of task 756 is that inference is what failed.
        """
        self.work()
        prompt = self.spawn.calls[0]["argv"][-1]
        self.assertIn(str(self.workdir / ".claude" / "worktrees" / "task-8"), prompt)
        self.assertIn("worktree-task-8", prompt)
        self.assertIn("Do not switch branches", prompt)
        self.assertIn("do not\n   merge into main", prompt)

    def test_a_ticket_with_no_board_number_falls_back_to_the_row_id(self):
        """And says so in the name, because the two numbering spaces overlap.

        A bare `task-91` would be indistinguishable from board number 91, which
        is a different, real ticket.
        """
        self.vikunja.layout["Ready"].append(
            task(NUMBERLESS_TASK, "Indexless", "2026-07-26T07:00:00Z", "body",
                 index=None)
        )
        self.service.work(self.service.get_task(NUMBERLESS_TASK))
        cwd = Path(self.spawn.calls[0]["cwd"])
        self.assertEqual(cwd.name, f"row-{NUMBERLESS_TASK}")


class AnExistingWorktreeIsReused(ServiceTestCase):
    def work(self):
        return self.service.work(self.service.get_by_task_number(READY_TICKET))

    def test_a_relaunch_continues_in_what_the_last_run_left(self):
        """The reason reuse is load-bearing rather than an optimisation.

        A run that is orphaned (task 754) or stopped leaves partial work in its
        worktree. Recreating would either fail or start from a clean tree, and
        the partial work would be silently discarded — which is exactly the loss
        the runner is supposed to have stopped causing.
        """
        self.work()
        worktree = Path(self.spawn.calls[0]["cwd"])
        (worktree / "half-done.txt").write_text("partial work\n", encoding="utf-8")

        # The first run's PID is not in `alive_pids`, so its lock reconciles and
        # the ticket can be launched again — the relaunch-after-orphan case.
        self.work()

        self.assertEqual(Path(self.spawn.calls[1]["cwd"]), worktree)
        self.assertEqual((worktree / "half-done.txt").read_text(), "partial work\n")

    def test_a_branch_that_outlived_its_worktree_is_attached_not_rebranched(self):
        """Its commits are the reason. Branching from main again abandons them.

        `git worktree remove` keeps the branch, so this is the state left by
        anyone tidying up a directory without deleting the work in it.
        """
        self.work()
        worktree = Path(self.spawn.calls[0]["cwd"])
        (worktree / "kept.txt").write_text("committed work\n", encoding="utf-8")
        git(worktree, "add", "kept.txt")
        git(worktree, "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-qm", "work from the first run")
        kept = git(worktree, "rev-parse", "HEAD")
        git(self.workdir, "worktree", "remove", "--force", str(worktree))
        self.assertFalse(worktree.exists())

        self.work()

        self.assertEqual(Path(self.spawn.calls[1]["cwd"]), worktree)
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), kept)
        self.assertEqual((worktree / "kept.txt").read_text(), "committed work\n")


class AWorktreeThatCannotBeMadeRefusesTheLaunch(ServiceTestCase):
    """Never a fallback to the repository root. That fallback IS the defect."""

    def setUp(self) -> None:
        super().setUp()
        self._plain = tempfile.TemporaryDirectory()
        self.addCleanup(self._plain.cleanup)
        # A directory that is not a git repository at all.
        self.config = make_config(self.state_dir, workdir=Path(self._plain.name))
        self.service.config = self.config
        self.launcher.config = self.config

    def test_the_launch_raises_rather_than_running_in_the_workdir(self):
        with self.assertRaises(LaunchError):
            self.service.work(self.service.get_by_task_number(READY_TICKET))
        self.assertEqual(self.spawn.calls, [])

    def test_nothing_is_left_holding_the_lock(self):
        """Or the ticket could never be launched again without a manual repair."""
        with self.assertRaises(LaunchError):
            self.service.work(self.service.get_by_task_number(READY_TICKET))
        self.assertFalse(
            (self.config.lock_dir / f"task-{READY_ROW_ID}.json").exists()
        )

    def test_the_refusal_is_recorded_where_launches_are(self):
        """A refusal nobody can see reads the same as a launch that never happened."""
        with self.assertRaises(LaunchError):
            self.service.work(self.service.get_by_task_number(READY_TICKET))
        events = [
            json.loads(line)
            for line in self.config.log_path.read_text().splitlines()
        ]
        self.assertEqual([e["event"] for e in events], ["launch_failed"])
        self.assertEqual(events[0]["reference"], f"#{READY_TICKET}")
        self.assertIn("worktree", events[0]["error"])


class TheHelperItself(unittest.TestCase):
    """The cases the launch path cannot reach on its own."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = make_repo(Path(self._tmp.name) / "repo")

    def test_calling_twice_returns_the_same_worktree(self):
        """Two requests for one ticket can both arrive before either takes the
        lock. Reuse is what makes that harmless; the lock decides the winner."""
        first = ensure_worktree(self.repo, 8, 9)
        second = ensure_worktree(self.repo, 8, 9)
        self.assertEqual(first, second)

    def test_a_directory_git_does_not_know_about_is_refused(self):
        """Rather than run in it. A leftover directory with no `.git` is not a
        worktree, and treating it as one would put the run somewhere unversioned
        where nothing it did could be recovered by branch."""
        stray = self.repo / ".claude" / "worktrees" / "task-8"
        stray.mkdir(parents=True)
        with self.assertRaises(WorktreeError):
            ensure_worktree(self.repo, 8, 9)


if __name__ == "__main__":
    unittest.main()
