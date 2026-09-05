"""Build the prompt for a single Vikunja ticket.

Harness-neutral, and that is load-bearing rather than incidental: the commit and
the report-back are instructions in this text and a helper script the child
runs, not capabilities of any particular CLI. It is what let task 810 change the
local executor's harness without the runner losing either of them, and it is why
both executors are handed the same prompt with nothing trimmed for being a local
model.

The prompt never contains the Vikunja API token. The run updates the board
through ``vkctl.py``, which reads the token from its own environment.

**Rule 2 is a working ORDER, not a preference** (task 823). The local Qwen run
for #813 finished its implementation and then lost substantial time to
overlapping test edits — malformed text, conflicting fixtures and undefined
helpers — because nothing was validated between them. The order is stated once,
here, for every executor: a harness-specific version of it would be a second
policy, and the run that needs it most is whichever one is cheapest to blame.
Note what it does NOT ask for: a full suite after every edit. The narrow checks
are what must follow each edit, and making the expensive one mandatory is how a
run learns to skip the step entirely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

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
{comments}
Rules for this run:

1. SCOPE. Do only what ticket {reference} asks. Do not fix unrelated bugs, do
   not refactor code the ticket does not touch, and do not start other tickets.
   If you find a separate problem, mention it in your completion comment rather
   than fixing it.
2. HOW YOU WORK. One change at a time, and validate it before making the next
   one. In this order, every time:
     a. Inspect, and identify the smallest coherent change.
     b. Apply that one change.
     c. Immediately syntax- or type-check the files you just touched.
     d. Run the narrowest existing tests that cover them.
     e. Fix any failure — a bad edit, a syntax or LSP error, a failing test —
        before you make any further change.
     f. Add regression tests the same way, one at a time, running each as you
        add it.
     g. Run the broader suite only once the narrow checks pass.
   You do not need the full suite after every edit; you do need step c and step
   d after every edit. Stacking unvalidated edits is how a run loses an hour to
   malformed text, conflicting fixtures and helpers that were never defined —
   and each of those is cheapest to find in the edit that caused it.
3. TESTS. The change is not finished without tests. Add or extend tests that
   would fail without your change, and run the relevant suite. Never weaken,
   skip or delete an existing test or assertion to make a failure disappear.
4. WHERE YOU ARE. This directory is a git worktree made for this ticket, on
   branch {branch}. Work here and commit here. Do not switch branches, do not
   merge into main, and do not make another worktree. Merging is a human step
   that happens after this run ends.
5. COMMIT. Commit your work with a message referencing the ticket, e.g.
   "<type>: <what changed> {commit_ref}". Commit only the files this ticket
   required.
6. DO NOT PUSH. No `git push`, no pull request, no remote of any kind. Leave the
   commit local.
7. REPORT BACK to Vikunja when you stop, using the helper below. It names the
   ticket the way the board does — never the number in a /tasks/<id> URL, which
   is a different number for a different task. Copy the commands as they stand.
   Do not call the Vikunja API directly and do not look for an API token — the
   helper already has what it needs.

   If you finished the ticket:
       python3 {vkctl} comment {selector} "<what you changed, which tests you ran, the commit sha, and that the commit is on branch {branch} and still has to be merged into main before this ticket is Done>"
       python3 {vkctl} move {selector} Waiting

   Waiting, not Done: work that is committed only on {branch} is not merged,
   and merging is the human step rule 4 names. Done belongs to whoever merges
   it into main, after this run has ended.

   If you are blocked and cannot finish:
       python3 {vkctl} comment {selector} "BLOCKED: <what is blocking you and what you need>"
       python3 {vkctl} move {selector} Waiting

   Comment first, then move — the comment is the part a human needs.

Start by reading the repository's CLAUDE.md and the files the ticket names.
"""


COMMENTS_TEMPLATE = """
--- BEGIN TICKET COMMENTS ({count}, oldest first) ---
These are the ticket as it stands NOW, and the description above is as it was
filed. A comment is where the filer corrects, redirects or REJECTS the brief
after it was written, so a later comment overrides an earlier one and overrides
the description. If one of them rejects work an earlier run already did, that
rejection is what this run is for -- re-checking the rejected work and
reporting it again is not.

{body}
--- END TICKET COMMENTS ---
"""


def _rendered_comment(position: int, total: int, comment: dict[str, Any]) -> str:
    """One comment, with enough around it to place it in the sequence.

    Author and time are stated because "who said this, and was it before or
    after the run that claimed to finish" is the question a continuation run is
    actually asking. Each is labelled when Vikunja did not send one rather than
    left blank, so a missing author cannot read as the previous line's.
    """
    author = comment.get("author") or "an unknown author"
    created = comment.get("created") or "an unknown time"
    text = (comment.get("text") or "").strip() or "(empty comment)"
    return f"[comment {position} of {total}] {author} at {created}:\n{text}"


def _comments_section(comments: Sequence[dict[str, Any]]) -> str:
    """The comment block for the prompt, or nothing at all when there are none.

    Order is whatever ``VikunjaClient.comment_views`` returned -- oldest first,
    the same sequence the /task page and the MCP publish. It is not re-sorted
    here: the sequence is one fact about a ticket, and a second definition of
    it in the prompt could disagree with the one a human read on the page.

    An uncommented ticket gets the prompt it has always had. Absence is not
    ambiguous, because a run only ever sees this prompt when the comments were
    read successfully -- ``TicketService`` reads them before anything is moved
    or launched, and a read that fails refuses the launch.
    """
    if not comments:
        return ""
    total = len(comments)
    body = "\n\n".join(
        _rendered_comment(position, total, comment)
        for position, comment in enumerate(comments, start=1)
    )
    return COMMENTS_TEMPLATE.format(count=total, body=body)


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
    comments: Sequence[dict[str, Any]],
    vkctl_path: Path = VKCTL,
) -> str:
    """The complete brief for one run: the ticket, and everything said on it.

    ``comments`` has no default on purpose (task 818). The defect this closes
    was a relaunch that received the description alone, revalidated the commit
    a review had already rejected and sent the ticket back to Waiting -- and a
    default of ``[]`` would let any future caller reintroduce exactly that, in
    the one shape where it looks like there was nothing to say.
    """
    description = ticket.description.strip() or "(no description on the ticket)"
    return TEMPLATE.format(
        project=project_title,
        comments=_comments_section(comments),
        reference=ticket.board_reference,
        commit_ref=ticket.commit_ref,
        summary=ticket.summary,
        selector=_selector(ticket),
        workdir=workdir,
        branch=branch_name(ticket.task_number, ticket.task_id),
        description=description,
        vkctl=vkctl_path,
    )
