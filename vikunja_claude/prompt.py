"""Build the prompt for a single Vikunja ticket.

Harness-neutral, and that is load-bearing rather than incidental: the commit and
the report-back are instructions in this text and a helper script the child
runs, not capabilities of any particular CLI. It is what let task 810 change the
local executor's harness without the runner losing either of them, and it is why
both executors are handed the same prompt with nothing trimmed for being a local
model.

The prompt never contains the Vikunja API token. The run updates the board
through ``vkctl.py``, which reads the token from its own environment.
"""

from __future__ import annotations

from pathlib import Path

from .config import PACKAGE_ROOT
from .vikunja import Ticket
from .worktree import branch_name

VKCTL = PACKAGE_ROOT / "vkctl.py"

TEMPLATE = """\
You are working a single ticket from the Vikunja board "{project}".

TICKET {reference}: {summary}
Repository: {workdir}

--- BEGIN TICKET DESCRIPTION ---
{description}
--- END TICKET DESCRIPTION ---

Rules for this run:

1. SCOPE. Do only what ticket {reference} asks. Do not fix unrelated bugs, do
   not refactor code the ticket does not touch, and do not start other tickets.
   If you find a separate problem, mention it in your completion comment rather
   than fixing it.
2. TESTS. The change is not finished without tests. Add or extend tests that
   would fail without your change, and run the relevant suite. Never weaken,
   skip or delete an existing test or assertion to make a failure disappear.
3. WHERE YOU ARE. This directory is a git worktree made for this ticket, on
   branch {branch}. Work here and commit here. Do not switch branches, do not
   merge into main, and do not make another worktree. Merging is a human step
   that happens after this run ends.
4. COMMIT. Commit your work with a message referencing the ticket, e.g.
   "<type>: <what changed> {commit_ref}". Commit only the files this ticket
   required.
5. DO NOT PUSH. No `git push`, no pull request, no remote of any kind. Leave the
   commit local.
6. REPORT BACK to Vikunja when you stop, using the helper below. It names the
   ticket the way the board does — never the number in a /tasks/<id> URL, which
   is a different number for a different task. Copy the commands as they stand.
   Do not call the Vikunja API directly and do not look for an API token — the
   helper already has what it needs.

   If you finished the ticket:
       python3 {vkctl} comment {selector} "<what you changed, which tests you ran, the commit sha>"
       python3 {vkctl} move {selector} Done

   If you are blocked and cannot finish:
       python3 {vkctl} comment {selector} "BLOCKED: <what is blocking you and what you need>"
       python3 {vkctl} move {selector} Waiting

   Comment first, then move — the comment is the part a human needs.

Start by reading the repository's CLAUDE.md and the files the ticket names.
"""


def _selector(ticket: Ticket) -> str:
    """How the run should address this ticket on the command line.

    The board number, which is what the ticket IS (task 659). ``--task`` appears
    only for a task Vikunja reported no index for — the one case where there is
    no board number to name, and the same case ``board_reference`` falls back
    on. It is not an alternative spelling of the number: the two spaces overlap,
    so a board number passed to ``--task`` silently reaches a different real
    ticket rather than failing.
    """
    if ticket.task_number is not None:
        return f"--number {ticket.task_number}"
    return f"--task {ticket.task_id}"


def build_prompt(
    ticket: Ticket,
    workdir: Path,
    project_title: str,
    vkctl_path: Path = VKCTL,
) -> str:
    description = ticket.description.strip() or "(no description on the ticket)"
    return TEMPLATE.format(
        project=project_title,
        reference=ticket.board_reference,
        commit_ref=ticket.commit_ref,
        summary=ticket.summary,
        selector=_selector(ticket),
        workdir=workdir,
        branch=branch_name(ticket.task_number, ticket.task_id),
        description=description,
        vkctl=vkctl_path,
    )
