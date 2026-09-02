"""Where a run works: one git worktree per ticket, made by the runner.

The runner owns the working directory as it owns the lock, the log and the
reap. It did not always. ``launch`` spawned with the repository root as its
cwd and nothing here created anything, so whether a run was isolated depended
entirely on the launched model choosing to isolate itself. Claude Code driving
Opus usually did; the local seat did not, and task 714's run spent an hour and
a half editing the main checkout beside a human editing the same files. Task
690 already called this "the existing worktree-based ticket runner" and
``executors.py`` still says the harness supplies "its worktree mode, the branch
it makes" — both were true only by the model's good manners (task 756).

**Naming deliberately differs from the lock's, and the difference is the
point.** The lock is keyed by the immutable Vikunja row id, because two runs of
one ticket must never both hold it: that is a correctness requirement and it
needs a key that cannot change. A worktree is a directory a human will ``cd``
into and a branch a human will merge, so it is named by the BOARD number — what
the board shows, what every worktree already in the investment checkout uses,
and the only number a person asking "where did #714's run go" has. The row id
appears only for a ticket Vikunja reported no index for, spelled ``row-`` so the
two numbering spaces can never be read as one.

**So ``run_name`` is what one run is CALLED, not just where it works
(task 762).** The run log was the last human-facing artifact still named from
the row id: board ticket #714's output went to ``task-715-<stamp>.log``, a
filename naming a different, real ticket, sitting one directory away from the
``task-714`` worktree the same run was working in. Two spellings of one run is
how a reader quotes the wrong number, which is the whole of task 659. The
worktree, the branch and the run log now come from this one function, so they
cannot disagree; the lock keeps the row id and keeps it internal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

#: Where worktrees go, relative to the repository root. This matches the
#: convention already in use in the investment checkout, and that path is
#: gitignored there — so a worktree never shows up as untracked content of the
#: repository containing it.
WORKTREES_DIR = Path(".claude") / "worktrees"

#: Prefixed so ``git branch`` groups them, and so a branch the runner made is
#: never mistaken for one a person made.
BRANCH_PREFIX = "worktree-"

#: What a run's worktree is branched from. LOCAL ``main``, never
#: ``origin/main``: origin is only ever as fresh as the last fetch, so branching
#: from it would silently start runs from an ageing base.
BASE_BRANCH = "main"


class WorktreeError(RuntimeError):
    """The worktree could not be prepared, so the run must not start.

    Always a refusal, never a fallback to the repository root. The fallback is
    precisely the defect: running in the root is what this module exists to
    stop, so "the worktree failed, so I used the checkout" would reintroduce it
    at exactly the moment nobody is watching.
    """


def run_name(task_number: int | None, task_id: int) -> str:
    """What one ticket's run is called: its directory, branch and log stem."""
    if task_number is not None:
        return f"task-{task_number}"
    return f"row-{task_id}"


def branch_name(task_number: int | None, task_id: int) -> str:
    return f"{BRANCH_PREFIX}{run_name(task_number, task_id)}"


def _git(run: Callable[..., subprocess.CompletedProcess], repo_root: Path, *args: str):
    return run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )


def ensure_worktree(
    repo_root: Path,
    task_number: int | None,
    task_id: int,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path:
    """The worktree this ticket's run works in, creating it only if needed.

    Reuse rather than recreate, and that is load-bearing rather than an
    optimisation: it is what lets the relaunch of a run that was orphaned
    (task 754) or stopped continue from what the last attempt had already done,
    instead of starting from a clean tree and silently discarding it.

    Reuse is also what makes this safe to call twice concurrently. Two requests
    for one ticket can both arrive here before either takes the lock; one
    creates and the other reuses, and the lock — not this — decides which run
    actually starts.
    """
    name = run_name(task_number, task_id)
    path = repo_root / WORKTREES_DIR / name

    # A registered worktree carries a `.git` FILE pointing at the parent's
    # metadata. Testing for that rather than for the directory means a leftover
    # directory git no longer knows about is rebuilt instead of being run in as
    # though it were a worktree.
    if (path / ".git").exists():
        return path

    if path.exists():
        raise WorktreeError(
            f"{path} exists but is not a git worktree; remove it or recover it "
            f"by hand — refusing to run a ticket in an unknown directory"
        )

    branch = branch_name(task_number, task_id)
    existing_branch = _git(
        run, repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
    )
    if existing_branch.returncode == 0:
        # The branch outlived its worktree — a previous one was removed while
        # its commits were kept. Attach to the branch rather than branching
        # again from main, which would abandon those commits where nothing
        # names them.
        result = _git(run, repo_root, "worktree", "add", str(path), branch)
    else:
        result = _git(
            run, repo_root, "worktree", "add", "-b", branch, str(path), BASE_BRANCH
        )

    if result.returncode != 0:
        raise WorktreeError(
            f"could not create worktree {path} on {branch}: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    return path
