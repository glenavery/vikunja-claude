"""Build the Claude Code prompt for a single Vikunja ticket.

The prompt never contains the Vikunja API token. Claude updates the board
through ``vkctl.py``, which reads the token from its own environment.
"""

from __future__ import annotations

from pathlib import Path

from .config import PACKAGE_ROOT
from .vikunja import Ticket

VKCTL = PACKAGE_ROOT / "vkctl.py"

TEMPLATE = """\
You are working a single ticket from the Vikunja board "{project}".

TICKET {reference}: {summary}
Vikunja task id: {task_id}
Vikunja URL: {url}
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
3. COMMIT. Commit your work with a message referencing the ticket, e.g.
   "<type>: <what changed> {commit_ref}". Commit only the files this ticket
   required.
4. DO NOT PUSH. No `git push`, no pull request, no remote of any kind. Leave the
   commit local.
5. REPORT BACK to Vikunja when you stop, using the helper below. It takes the
   task id, not the #NN prefix. Do not call the Vikunja API directly and do not
   look for an API token — the helper already has what it needs.

   If you finished the ticket:
       python3 {vkctl} comment --task {task_id} "<what you changed, which tests you ran, the commit sha>"
       python3 {vkctl} move --task {task_id} Done

   If you are blocked and cannot finish:
       python3 {vkctl} comment --task {task_id} "BLOCKED: <what is blocking you and what you need>"
       python3 {vkctl} move --task {task_id} Waiting

   Comment first, then move — the comment is the part a human needs.

Start by reading the repository's CLAUDE.md and the files the ticket names.
"""


def build_prompt(
    ticket: Ticket,
    workdir: Path,
    project_title: str,
    frontend_url: str,
    vkctl_path: Path = VKCTL,
) -> str:
    description = ticket.description.strip() or "(no description on the ticket)"
    return TEMPLATE.format(
        project=project_title,
        reference=ticket.reference,
        commit_ref=ticket.commit_ref,
        summary=ticket.summary,
        task_id=ticket.task_id,
        url=ticket.url(frontend_url),
        workdir=workdir,
        description=description,
        vkctl=vkctl_path,
    )
